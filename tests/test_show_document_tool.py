"""show_document: a link from the sender's MCP servers, or a file, kept with the conversation.

The tool runs for the turn's sender in the turn's conversation (SQLite here; the
sharing paths run on PostgreSQL in test_preview_sharing_web.py):

* it is a built-in tool in the documents category, so a server offers and runs it only
  for a user holding ``spark.tools.documents``;
* a link is fetched under the fetch guard from the host of an MCP server the sender is
  connected to (the stub link server stands in for KVASIR), and kept as a file of the
  conversation under the attachment limit and the sender's quota;
* a file is one of the conversation's (anyone's) or one of the sender's own (copied
  in); another person's file from elsewhere is refused;
* every refusal is a plain reason, and nothing is stored when it is refused;
* the result carries the card the chat page draws.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from spark import files as files_pkg
from spark.database import conversation_members, conversations
from spark.database import files as files_db
from spark.database.scope import Role
from spark.mcp_integration.client import MCPServerConfig
from spark.mcp_integration.manager import MCPManager
from spark.mode import Mode
from spark.preview import service, sources
from spark.preview.kinds import Kind
from spark.tools import registry
from spark.tools.registry import (
    PERMISSIONS_KEY,
    execute_builtin_tool,
    get_builtin_tools,
    tool_category,
)
from tests.preview_helpers import Fixture, LinkServer, pdf_bytes, png_bytes, text_bytes

ADA = "user-ada-0001"
CORA = "user-cora-0002"
SAM = "user-sam-0003"


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


@pytest.fixture
def bridge(spark_db_settings: Any) -> Iterator[Any]:
    from spark.db.standalone import open_standalone

    handle = open_standalone(spark_db_settings, auto_migrate=False)
    try:
        yield handle.sync_db
    finally:
        handle.close()


@pytest.fixture
def limits() -> dict[str, Any]:
    return {}


@pytest.fixture
def area(tmp_path: Path, limits: dict[str, Any]) -> Iterator[Path]:
    root = tmp_path / "volume"
    files_pkg.configure(
        _Settings({"files.root": str(root), **limits}),
        data_dir=tmp_path,
        runtime_mode=Mode.PERSONAL,
    )
    yield root
    files_pkg.reset()


@dataclass
class FakeClient:
    config: MCPServerConfig
    connected: bool = True

    @property
    def name(self) -> str:
        return self.config.name


def manager_for(*configs: MCPServerConfig, connected: bool = True) -> MCPManager:
    manager = MCPManager(client_factory=lambda c: FakeClient(c, connected=connected))
    for config in configs:
        manager.add_server(config)
    return manager


@pytest.fixture
def links() -> Iterator[LinkServer]:
    with LinkServer() as server:
        yield server


@pytest.fixture
def kvasir(links: LinkServer) -> Iterator[MCPManager]:
    """The sender is connected to one MCP server, on the link server's host and port."""
    manager = manager_for(
        MCPServerConfig(name="kvasir", transport="http", url=f"http://127.0.0.1:{links.port}/mcp")
    )
    sources.bind(lambda: manager)
    yield manager
    sources.unbind()


def conversation(bridge: Any, owner: str = ADA) -> int:
    return int(bridge.call(conversations.create_conversation, "Plans", "stub/model", owner))


def save(
    bridge: Any, user: str, name: str, data: bytes, cid: int | None, mime: str = "text/plain"
) -> int:
    return files_pkg.file_area(bridge).save(
        user,
        f"uploads/{name}",
        io.BytesIO(data),
        filename=name,
        mime_type=mime,
        conversation_id=cid,
    )


def config(bridge: Any, user: str | None, cid: int | None, **extra: Any) -> dict[str, Any]:
    return {
        "_user_guid": user,
        "_kg_db": bridge,
        "_kg_conversation_id": cid,
        "_kg_user_guid": user,
        **extra,
    }


def run(args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, bool]:
    return execute_builtin_tool("show_document", args, cfg)


def stored(bridge: Any, user: str) -> list[Any]:
    return list(bridge.call(files_db.list_user_files, user))


class TestCatalogue:
    def test_it_is_a_documents_tool(self) -> None:
        assert tool_category("show_document") == "documents"
        assert "show_document" in registry.TOOL_CATEGORIES["documents"]

    def test_it_is_offered_with_a_file_area(self, area: Path) -> None:
        names = {t["name"] for t in get_builtin_tools({"embedded_tools": {}})}
        assert "show_document" in names

    def test_a_server_offers_it_only_with_the_documents_permission(
        self, area: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(registry, "_current_mode", lambda: Mode.SERVER)
        with_it = get_builtin_tools({PERMISSIONS_KEY: {"spark.tools.documents"}})
        without = get_builtin_tools({PERMISSIONS_KEY: {"spark.tools.web"}})
        assert "show_document" in {t["name"] for t in with_it}
        assert "show_document" not in {t["name"] for t in without}

    def test_a_server_refuses_it_without_the_permission(
        self, area: Path, bridge: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(registry, "_current_mode", lambda: Mode.SERVER)
        cid = conversation(bridge)
        text, error = run(
            {"file_id": 1}, config(bridge, ADA, cid, **{PERMISSIONS_KEY: {"spark.tools.web"}})
        )
        assert error and "your role does not allow documents tools" in text

    def test_its_documentation_exists(self) -> None:
        from spark.tools.registry import _get_tool_documentation

        doc = _get_tool_documentation("show_document")
        assert doc.startswith("# show_document")
        assert "show_document" in _get_tool_documentation("_index")


class TestLinks:
    def test_a_link_from_the_mcp_server_is_kept_with_the_conversation(
        self, area: Path, bridge: Any, links: LinkServer, kvasir: MCPManager
    ) -> None:
        cid = conversation(bridge)
        body = pdf_bytes()
        url = links.add(
            "/document-links/v1/abc",
            Fixture(body, "application/pdf", 'inline; filename="Quarterly report.pdf"'),
        )
        text, error = run({"url": url, "title": "Q3 report"}, config(bridge, ADA, cid))
        assert not error, text
        card = service.parse_card(text)
        assert card is not None
        assert (card.conversation_id, card.name, card.title) == (
            cid,
            "Quarterly report.pdf",
            "Q3 report",
        )
        assert (card.kind, card.type, card.size) == ("pdf", "application/pdf", len(body))
        row = bridge.call(files_db.get_conversation_file, ADA, cid, card.file_id)
        assert row is not None and row["source"] == "preview" and row["user_guid"] == ADA
        with files_pkg.file_store().open(ADA, row["storage_key"]) as handle:
            assert handle.read() == body
        assert "Quarterly report.pdf (PDF" in text

    def test_another_host_is_refused_and_nothing_is_stored(
        self, area: Path, bridge: Any, links: LinkServer, kvasir: MCPManager
    ) -> None:
        cid = conversation(bridge)
        links.add("/d", Fixture(b"x", "text/plain"))
        text, error = run({"url": links.url("/d", host="localhost")}, config(bridge, ADA, cid))
        assert error and text.startswith("The document was not shown:")
        assert "hosts of MCP servers you are connected to" in text
        assert stored(bridge, ADA) == [] and links.requests == []

    def test_a_disconnected_server_allows_no_link(
        self, area: Path, bridge: Any, links: LinkServer
    ) -> None:
        manager = manager_for(
            MCPServerConfig(name="k", transport="http", url=f"http://127.0.0.1:{links.port}/m"),
            connected=False,
        )
        sources.bind(lambda: manager)
        try:
            cid = conversation(bridge)
            text, error = run({"url": links.url("/d")}, config(bridge, ADA, cid))
        finally:
            sources.unbind()
        assert error and "connected to none" in text

    def test_a_server_the_senders_roles_do_not_grant_allows_no_link(
        self,
        area: Path,
        bridge: Any,
        links: LinkServer,
        kvasir: MCPManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(registry, "_current_mode", lambda: Mode.SERVER)
        cid = conversation(bridge)
        url = links.add("/d", Fixture(b"x", "text/plain"))
        refused, error = run(
            {"url": url},
            config(bridge, ADA, cid, **{PERMISSIONS_KEY: {"spark.tools.documents"}}),
        )
        assert error and "connected to none" in refused
        granted, error = run(
            {"url": url},
            config(
                bridge,
                ADA,
                cid,
                **{PERMISSIONS_KEY: {"spark.tools.documents", "spark.mcp.kvasir"}},
            ),
        )
        assert not error, granted

    def test_a_stdio_server_has_no_host(self, area: Path) -> None:
        manager = manager_for(MCPServerConfig(name="local", transport="stdio", command="x"))
        assert sources.mcp_origins(manager, None) == []

    def test_an_oversize_link_is_refused_under_the_attachment_limit(
        self, bridge: Any, links: LinkServer, kvasir: MCPManager, tmp_path: Path
    ) -> None:
        files_pkg.configure(
            _Settings({"files.root": str(tmp_path / "v"), "files.max_upload_mb": 0.001}),
            data_dir=tmp_path,
            runtime_mode=Mode.PERSONAL,
        )
        try:
            cid = conversation(bridge)
            url = links.add("/big", Fixture(b"a" * 5000, "text/plain"))
            text, error = run({"url": url}, config(bridge, ADA, cid))
        finally:
            files_pkg.reset()
        assert error and "attachment limit of 1,048 bytes" in text
        assert stored(bridge, ADA) == []

    def test_the_senders_quota_applies(
        self, bridge: Any, links: LinkServer, kvasir: MCPManager, tmp_path: Path
    ) -> None:
        files_pkg.configure(
            _Settings({"files.root": str(tmp_path / "v"), "files.quota_mb": 0.002}),
            data_dir=tmp_path,
            runtime_mode=Mode.PERSONAL,
        )
        try:
            cid = conversation(bridge)
            url = links.add("/q", Fixture(b"a" * 3000, "text/plain"))
            text, error = run({"url": url}, config(bridge, ADA, cid))
        finally:
            files_pkg.reset()
        assert error and "Storage quota exceeded" in text
        assert stored(bridge, ADA) == []


class TestFiles:
    def test_a_file_of_the_conversation_is_shown_without_a_copy(
        self, area: Path, bridge: Any
    ) -> None:
        cid = conversation(bridge)
        fid = save(bridge, ADA, "notes.txt", text_bytes(), cid)
        text, error = run({"file_id": fid}, config(bridge, ADA, cid))
        assert not error, text
        card = service.parse_card(text)
        assert card is not None and card.file_id == fid and card.kind == "text"
        assert len(stored(bridge, ADA)) == 1

    def test_another_members_file_in_the_conversation_is_shown(
        self, area: Path, bridge: Any
    ) -> None:
        cid = conversation(bridge)
        bridge.call(conversation_members.share, ADA, cid, CORA, Role.CONTRIBUTOR)
        fid = save(bridge, ADA, "pic.png", png_bytes(), cid, mime="image/png")
        text, error = run({"file_id": fid}, config(bridge, CORA, cid))
        assert not error, text
        card = service.parse_card(text)
        assert card is not None and card.file_id == fid and card.kind == "image"

    def test_the_senders_own_file_from_elsewhere_is_copied_in(
        self, area: Path, bridge: Any
    ) -> None:
        cid = conversation(bridge)
        fid = save(bridge, ADA, "plan.pdf", pdf_bytes(1), None, mime="application/pdf")
        text, error = run({"file_id": str(fid)}, config(bridge, ADA, cid))
        assert not error, text
        card = service.parse_card(text)
        assert card is not None and card.file_id != fid and card.kind == "pdf"
        copy = bridge.call(files_db.get_conversation_file, ADA, cid, card.file_id)
        assert copy["source"] == "preview" and copy["filename"] == "plan.pdf"

    def test_another_persons_file_from_elsewhere_is_refused(self, area: Path, bridge: Any) -> None:
        cid = conversation(bridge)
        elsewhere = conversation(bridge, owner=SAM)
        fid = save(bridge, SAM, "secret.txt", b"private", elsewhere)
        text, error = run({"file_id": fid}, config(bridge, ADA, cid))
        assert error and f"File {fid} is not one of this conversation's files or yours" in text
        assert stored(bridge, ADA) == []

    def test_another_persons_file_from_a_conversation_shared_with_the_sender_is_refused(
        self, area: Path, bridge: Any
    ) -> None:
        # Ada may read Sam's file where Sam shared it with her, but showing it in her own
        # conversation would hand it to people Sam never gave it to.
        mine = conversation(bridge)
        sams = conversation(bridge, owner=SAM)
        bridge.call(conversation_members.share, SAM, sams, ADA, Role.VIEWER)
        fid = save(bridge, SAM, "sams.txt", b"for Ada only", sams)
        text, error = run({"file_id": fid}, config(bridge, ADA, mine))
        assert error and "is not one of this conversation's files or yours" in text
        assert stored(bridge, ADA) == []
        # In Sam's conversation itself, Ada may show it to its members.
        text, error = run({"file_id": fid}, config(bridge, ADA, sams))
        assert not error, text

    def test_a_file_whose_content_is_not_its_type_is_only_offered(
        self, area: Path, bridge: Any
    ) -> None:
        cid = conversation(bridge)
        fid = save(bridge, ADA, "fake.pdf", b"<html>not a pdf</html>", cid, mime="application/pdf")
        text, error = run({"file_id": fid}, config(bridge, ADA, cid))
        card = service.parse_card(text)
        assert not error and card is not None and card.kind == Kind.OTHER.value

    @pytest.mark.parametrize(
        ("args", "reason"),
        [
            ({}, "needs either a url or a file_id"),
            ({"url": "https://a.example/x", "file_id": 3}, "not both"),
            ({"file_id": "three"}, "file_id must be a file id"),
            ({"file_id": True}, "file_id must be a file id"),
        ],
    )
    def test_bad_arguments(
        self, area: Path, bridge: Any, args: dict[str, Any], reason: str
    ) -> None:
        cid = conversation(bridge)
        text, error = run(args, config(bridge, ADA, cid))
        assert error and reason in text

    def test_no_user_or_conversation_is_refused(self, area: Path, bridge: Any) -> None:
        text, error = run({"file_id": 1}, config(bridge, None, 1))
        assert error and "signed-in user in a conversation" in text
        text, error = run({"file_id": 1}, config(bridge, ADA, None))
        assert error and "signed-in user in a conversation" in text


class TestCard:
    def test_the_card_line_round_trips(self) -> None:
        card = service.Card(7, 3, "a.pdf", "A", "application/pdf", "pdf", 10)
        assert service.parse_card(f"Shown.\n{service.card_line(card)}") == card

    @pytest.mark.parametrize(
        "text", ["", "no card", "[spark-preview] not json", '[spark-preview] {"file_id": 1}']
    )
    def test_no_card(self, text: str) -> None:
        assert service.parse_card(text) is None

    def test_names_are_made_plain(self) -> None:
        assert service.safe_name("../../etc/passwd") == "passwd"
        assert service.safe_name('a<script>"b.pdf') == "a_script__b.pdf"
        assert service.safe_name("") == "document"
