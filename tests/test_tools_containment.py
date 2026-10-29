"""Document, document creation and archive tools stay inside their permitted area.

Server mode: the signed-in user's area, by area-relative key, with created and
extracted files charged to the user's quota. Personal mode: the configured
allowed roots. Every escape form in ``tests.test_files_paths.ESCAPES`` is tried
against every path argument of every tool.
"""

from __future__ import annotations

import io
import os
import stat
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spark.database import files as files_db
from spark.files.area import FileArea, FileLimits
from spark.files.paths import PathRefused
from spark.files.scope import ToolScope, ToolScopeUnavailable, tool_scope
from spark.files.store import MountedVolumeStore
from spark.tools import archives, document_creation, documents
from tests.test_files_paths import ESCAPES

ALICE = "user-alice-0001"
BOB = "user-bob-0002"
ESCAPE_KEYS = [k for _, k in ESCAPES]
ESCAPE_IDS = [i for i, _ in ESCAPES]


@pytest.fixture
def bridge(spark_db_settings: Any) -> Iterator[Any]:
    from spark.db.standalone import open_standalone

    handle = open_standalone(spark_db_settings, auto_migrate=False)
    try:
        yield handle.sync_db
    finally:
        handle.close()


@pytest.fixture
def limits() -> dict[str, int | None]:
    return {"quota": None}


@pytest.fixture
def area(tmp_path: Path, bridge: Any, limits: dict[str, int | None]) -> FileArea:
    return FileArea(
        MountedVolumeStore(tmp_path / "volume"),
        bridge,
        lambda: FileLimits(max_upload_bytes=None, quota_bytes=limits["quota"]),
    )


@pytest.fixture
def server(area: FileArea) -> ToolScope:
    return ToolScope.server(area, ALICE, conversation_id=None)


@pytest.fixture
def personal(tmp_path: Path) -> ToolScope:
    root = tmp_path / "allowed"
    root.mkdir()
    return ToolScope.personal([root])


def _docx(path: Path, text: str = "Synthetic paragraph") -> None:
    from docx import Document

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))


def _zip(path: Path, members: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def _zip_symlink(path: Path, name: str, target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo(name)
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, target)


def _tar(path: Path, members: list[tarfile.TarInfo], data: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tf:
        for info in members:
            payload = data.get(info.name)
            if payload is not None:
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
            else:
                tf.addfile(info)


# -- Scope construction ---------------------------------------------------------


class TestToolScopeFromConfig:
    def test_personal_roots_from_allowed_paths(self, tmp_path: Path) -> None:
        config = {"embedded_tools": {"filesystem": {"allowed_paths": f"{tmp_path}, "}}}
        scope = tool_scope(config, server=False)
        assert scope.roots == (tmp_path,)
        assert not scope.is_server

    def test_personal_without_roots_is_unavailable(self) -> None:
        with pytest.raises(ToolScopeUnavailable):
            tool_scope({"embedded_tools": {"filesystem": {"allowed_paths": []}}}, server=False)

    def test_server_without_a_user_is_unavailable(self, bridge: Any) -> None:
        with pytest.raises(ToolScopeUnavailable):
            tool_scope({"_files_db": bridge}, server=True)

    def test_server_ignores_allowed_paths(self, tmp_path: Path, bridge: Any) -> None:
        from spark import files as files_pkg
        from spark.mode import Mode

        files_pkg.configure(
            _Settings({"files.root": str(tmp_path / "vol")}),
            data_dir=tmp_path,
            runtime_mode=Mode.SERVER,
        )
        try:
            config = {
                "embedded_tools": {"filesystem": {"allowed_paths": [str(tmp_path)]}},
                "_user_guid": ALICE,
                "_files_db": bridge,
                "_kg_conversation_id": 3,
            }
            scope = tool_scope(config, server=True)
        finally:
            files_pkg.reset()
        assert scope.is_server
        assert scope.roots == (scope.area_root,)
        assert scope.area_root.is_relative_to((tmp_path / "vol").resolve())
        assert scope.conversation_id == 3

    def test_server_falls_back_to_the_knowledge_graph_user(
        self, tmp_path: Path, bridge: Any
    ) -> None:
        from spark import files as files_pkg
        from spark.mode import Mode

        files_pkg.configure(
            _Settings({"files.root": str(tmp_path / "vol")}),
            data_dir=tmp_path,
            runtime_mode=Mode.SERVER,
        )
        try:
            scope = tool_scope({"_kg_user_guid": BOB, "_kg_db": bridge}, server=True)
        finally:
            files_pkg.reset()
        assert scope.user_guid == BOB


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


# -- Document reading -----------------------------------------------------------


class TestReadDocuments:
    @pytest.mark.parametrize("key", ESCAPE_KEYS, ids=ESCAPE_IDS)
    def test_server_escape_forms_are_refused(self, server: ToolScope, key: str) -> None:
        with pytest.raises(PathRefused):
            documents.execute("read_word", {"path": key}, scope=server)

    def test_server_reads_by_key(self, server: ToolScope, area: FileArea) -> None:
        _docx(area.store.local_path(ALICE, "uploads/1/a.docx"))
        out = documents.execute("read_word", {"path": "uploads/1/a.docx"}, scope=server)
        assert "Synthetic paragraph" in out

    def test_server_cannot_read_another_users_area(
        self, area: FileArea, server: ToolScope, tmp_path: Path
    ) -> None:
        _docx(area.store.local_path(BOB, "b.docx"), "Bob only")
        bob_path = str(area.store.local_path(BOB, "b.docx"))
        with pytest.raises(PathRefused):
            documents.execute("read_word", {"path": bob_path}, scope=server)
        out = documents.execute("read_word", {"path": "b.docx"}, scope=server)
        assert "Bob only" not in out and "not found" in out.lower()

    def test_server_messages_do_not_reveal_the_volume(
        self, server: ToolScope, tmp_path: Path
    ) -> None:
        out = documents.execute("read_word", {"path": "missing.docx"}, scope=server)
        assert str(tmp_path) not in out and "missing.docx" in out

    def test_personal_reads_inside_a_root(self, personal: ToolScope) -> None:
        target = personal.roots[0] / "a.docx"
        _docx(target)
        out = documents.execute("read_word", {"path": str(target)}, scope=personal)
        assert "Synthetic paragraph" in out

    def test_personal_refuses_outside_the_roots(self, personal: ToolScope, tmp_path: Path) -> None:
        outside = tmp_path / "outside.docx"
        _docx(outside)
        with pytest.raises(PathRefused):
            documents.execute("read_word", {"path": str(outside)}, scope=personal)

    def test_personal_refuses_a_symlink_out_of_the_root(
        self, personal: ToolScope, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside.docx"
        _docx(outside)
        try:
            os.symlink(outside, personal.roots[0] / "link.docx")
        except OSError as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
        with pytest.raises(PathRefused):
            documents.execute(
                "read_word", {"path": str(personal.roots[0] / "link.docx")}, scope=personal
            )


# -- Document creation ----------------------------------------------------------

_CREATE_INPUTS: dict[str, dict[str, Any]] = {
    "create_word": {"content": [{"type": "paragraph", "text": "Synthetic"}]},
    "create_excel": {"sheets": [{"name": "S", "headers": ["a"], "rows": [[1]]}]},
    "create_powerpoint": {"slides": [{"layout": "title", "title": "Synthetic"}]},
    "create_pdf": {"content": [{"type": "paragraph", "text": "Synthetic"}]},
}
_SUFFIX = {
    "create_word": ".docx",
    "create_excel": ".xlsx",
    "create_powerpoint": ".pptx",
    "create_pdf": ".pdf",
}


class TestCreateDocuments:
    @pytest.mark.parametrize("key", ESCAPE_KEYS, ids=ESCAPE_IDS)
    def test_server_escape_forms_are_refused(self, server: ToolScope, key: str) -> None:
        with pytest.raises(PathRefused):
            document_creation.execute(
                "create_word", {"path": key, **_CREATE_INPUTS["create_word"]}, scope=server
            )

    @pytest.mark.parametrize("tool", sorted(_CREATE_INPUTS))
    def test_server_creates_in_the_area_and_records_it(
        self, tool: str, server: ToolScope, area: FileArea, bridge: Any, tmp_path: Path
    ) -> None:
        key = f"reports/out{_SUFFIX[tool]}"
        out = document_creation.execute(tool, {"path": key, **_CREATE_INPUTS[tool]}, scope=server)
        assert key in out and str(tmp_path) not in out
        assert area.store.local_path(ALICE, key).is_file()
        row = bridge.call(files_db.get_user_file_by_key, ALICE, key)
        assert row["source"] == "created"
        assert row["size_bytes"] == area.store.local_path(ALICE, key).stat().st_size

    def test_server_create_over_quota_is_refused(
        self, server: ToolScope, area: FileArea, limits: dict[str, int | None]
    ) -> None:
        limits["quota"] = 100
        with pytest.raises(files_db.QuotaExceeded):
            document_creation.execute(
                "create_pdf", {"path": "big.pdf", **_CREATE_INPUTS["create_pdf"]}, scope=server
            )
        assert area.store.list(ALICE) == []

    def test_server_image_outside_the_area_is_not_embedded(
        self, server: ToolScope, area: FileArea, tmp_path: Path
    ) -> None:
        from docx import Document

        image = tmp_path / "outside.png"
        image.write_bytes(_PNG)
        content = [{"type": "image", "image_path": str(image)}]
        document_creation.execute(
            "create_word", {"path": "img.docx", "content": content}, scope=server
        )
        doc = Document(str(area.store.local_path(ALICE, "img.docx")))
        assert len(doc.inline_shapes) == 0

    def test_server_image_inside_the_area_is_embedded(
        self, server: ToolScope, area: FileArea
    ) -> None:
        from docx import Document

        area.store.put(ALICE, "pics/dot.png", _PNG)
        content = [{"type": "image", "image_path": "pics/dot.png"}]
        document_creation.execute(
            "create_word", {"path": "img.docx", "content": content}, scope=server
        )
        doc = Document(str(area.store.local_path(ALICE, "img.docx")))
        assert len(doc.inline_shapes) == 1

    def test_personal_creates_inside_a_root(self, personal: ToolScope) -> None:
        target = personal.roots[0] / "sub" / "a.docx"
        document_creation.execute(
            "create_word", {"path": str(target), **_CREATE_INPUTS["create_word"]}, scope=personal
        )
        assert target.is_file()

    def test_personal_refuses_outside_the_roots(self, personal: ToolScope, tmp_path: Path) -> None:
        with pytest.raises(PathRefused):
            document_creation.execute(
                "create_word",
                {"path": str(tmp_path / "x.docx"), **_CREATE_INPUTS["create_word"]},
                scope=personal,
            )
        assert not (tmp_path / "x.docx").exists()


# -- Archives -------------------------------------------------------------------


class TestArchivePaths:
    @pytest.mark.parametrize("key", ESCAPE_KEYS, ids=ESCAPE_IDS)
    def test_server_archive_path_escapes_are_refused(self, server: ToolScope, key: str) -> None:
        with pytest.raises(PathRefused):
            archives.execute("list_archive", {"path": key}, mode="extract", scope=server)

    @pytest.mark.parametrize("key", ESCAPE_KEYS, ids=ESCAPE_IDS)
    def test_server_destination_escapes_are_refused(
        self, server: ToolScope, area: FileArea, key: str
    ) -> None:
        _zip(area.store.local_path(ALICE, "a.zip"), {"x.txt": b"x"})
        with pytest.raises(PathRefused):
            archives.execute(
                "extract_archive",
                {"path": "a.zip", "destination": key},
                mode="extract",
                scope=server,
            )

    def test_server_lists_by_key(self, server: ToolScope, area: FileArea) -> None:
        _zip(area.store.local_path(ALICE, "a.zip"), {"hello.txt": b"hi"})
        out = archives.execute("list_archive", {"path": "a.zip"}, mode="list", scope=server)
        assert "hello.txt" in out


# Member names that must never be written, as (id, name).
ZIP_SLIP = [
    ("parent", "../evil.txt"),
    ("parent-nested", "ok/../../evil.txt"),
    ("parent-backslash", "..\\evil.txt"),
    ("absolute", "/tmp/evil.txt"),
    ("drive", "C:\\evil.txt"),
    ("drive-relative", "C:evil.txt"),
    ("unc", "\\\\server\\share\\evil.txt"),
    ("ads", "good.txt:stream"),
    ("device", "CON"),
]


class TestArchiveExtraction:
    @pytest.mark.parametrize("member", [m for _, m in ZIP_SLIP], ids=[i for i, _ in ZIP_SLIP])
    def test_zip_member_escaping_the_target_refuses_the_whole_archive(
        self, server: ToolScope, area: FileArea, bridge: Any, member: str
    ) -> None:
        _zip(area.store.local_path(ALICE, "a.zip"), {"fine.txt": b"fine", member: b"evil"})
        with pytest.raises(PathRefused):
            archives.execute(
                "extract_archive",
                {"path": "a.zip", "destination": "out"},
                mode="extract",
                scope=server,
            )
        assert not area.store.local_path(ALICE, "out").exists()
        assert [f.key for f in area.store.list(ALICE)] == ["a.zip"]
        assert bridge.call(files_db.list_user_files, ALICE) == []

    @pytest.mark.parametrize("member", [m for _, m in ZIP_SLIP], ids=[i for i, _ in ZIP_SLIP])
    def test_tar_member_escaping_the_target_refuses_the_whole_archive(
        self, server: ToolScope, area: FileArea, member: str
    ) -> None:
        _tar(
            area.store.local_path(ALICE, "a.tar"),
            [tarfile.TarInfo("fine.txt"), tarfile.TarInfo(member)],
            {"fine.txt": b"fine", member: b"evil"},
        )
        with pytest.raises(PathRefused):
            archives.execute(
                "extract_archive",
                {"path": "a.tar", "destination": "out"},
                mode="extract",
                scope=server,
            )
        assert not area.store.local_path(ALICE, "out").exists()

    def test_zip_symlink_member_is_refused(self, server: ToolScope, area: FileArea) -> None:
        _zip_symlink(area.store.local_path(ALICE, "a.zip"), "link", "../../outside")
        with pytest.raises(PathRefused):
            archives.execute(
                "extract_archive",
                {"path": "a.zip", "destination": "out"},
                mode="extract",
                scope=server,
            )

    @pytest.mark.parametrize(
        "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE]
    )
    def test_tar_link_and_special_members_are_refused(
        self, server: ToolScope, area: FileArea, kind: bytes
    ) -> None:
        info = tarfile.TarInfo("special")
        info.type = kind
        info.linkname = "fine.txt"
        _tar(
            area.store.local_path(ALICE, "a.tar"),
            [tarfile.TarInfo("fine.txt"), info],
            {"fine.txt": b"fine"},
        )
        with pytest.raises(PathRefused):
            archives.execute(
                "extract_archive",
                {"path": "a.tar", "destination": "out"},
                mode="extract",
                scope=server,
            )

    def test_server_extracts_records_and_charges(
        self, server: ToolScope, area: FileArea, bridge: Any
    ) -> None:
        _zip(
            area.store.local_path(ALICE, "a.zip"),
            {"dir/": b"", "dir/one.txt": b"1", "two.txt": b"22"},
        )
        out = archives.execute(
            "extract_archive",
            {"path": "a.zip", "destination": "out"},
            mode="extract",
            scope=server,
        )
        assert "2 files" in out
        assert (area.store.local_path(ALICE, "out/dir/one.txt")).read_bytes() == b"1"
        keys = sorted(r["storage_key"] for r in bridge.call(files_db.list_user_files, ALICE))
        assert keys == ["out/dir/one.txt", "out/two.txt"]
        assert bridge.call(files_db.get_usage, ALICE) == 3

    def test_server_tar_extracts(self, server: ToolScope, area: FileArea) -> None:
        _tar(area.store.local_path(ALICE, "a.tar"), [tarfile.TarInfo("t.txt")], {"t.txt": b"t"})
        archives.execute(
            "extract_archive",
            {"path": "a.tar", "destination": "out"},
            mode="extract",
            scope=server,
        )
        assert area.store.local_path(ALICE, "out/t.txt").read_bytes() == b"t"

    def test_server_extraction_over_quota_is_refused_before_writing(
        self, server: ToolScope, area: FileArea, limits: dict[str, int | None]
    ) -> None:
        _zip(area.store.local_path(ALICE, "a.zip"), {"big.bin": b"x" * 5000})
        limits["quota"] = 1000
        with pytest.raises(files_db.QuotaExceeded):
            archives.execute(
                "extract_archive",
                {"path": "a.zip", "destination": "out"},
                mode="extract",
                scope=server,
            )
        assert not area.store.local_path(ALICE, "out").exists()

    def test_personal_extracts_inside_a_root(self, personal: ToolScope) -> None:
        root = personal.roots[0]
        _zip(root / "a.zip", {"x/y.txt": b"y"})
        archives.execute(
            "extract_archive",
            {"path": str(root / "a.zip"), "destination": str(root / "out")},
            mode="extract",
            scope=personal,
        )
        assert (root / "out" / "x" / "y.txt").read_bytes() == b"y"

    def test_personal_zip_slip_is_refused(self, personal: ToolScope, tmp_path: Path) -> None:
        root = personal.roots[0]
        _zip(root / "a.zip", {"../../escaped.txt": b"evil"})
        with pytest.raises(PathRefused):
            archives.execute(
                "extract_archive",
                {"path": str(root / "a.zip"), "destination": str(root / "out")},
                mode="extract",
                scope=personal,
            )
        assert not (tmp_path / "escaped.txt").exists()
        assert not (root / "out").exists()

    def test_personal_destination_outside_the_roots_is_refused(
        self, personal: ToolScope, tmp_path: Path
    ) -> None:
        root = personal.roots[0]
        _zip(root / "a.zip", {"x.txt": b"x"})
        with pytest.raises(PathRefused):
            archives.execute(
                "extract_archive",
                {"path": str(root / "a.zip"), "destination": str(tmp_path / "out")},
                mode="extract",
                scope=personal,
            )

    def test_extract_needs_extract_mode(self, personal: ToolScope) -> None:
        root = personal.roots[0]
        _zip(root / "a.zip", {"x.txt": b"x"})
        out = archives.execute(
            "extract_archive",
            {"path": str(root / "a.zip"), "destination": str(root / "out")},
            mode="list",
            scope=personal,
        )
        assert "not enabled" in out.lower()


def _png() -> bytes:
    """A valid 1x1 RGB PNG (synthetic)."""
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(b"\x00\xff\x00\x00")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


_PNG = _png()


# -- Registry wiring ------------------------------------------------------------


@pytest.fixture
def server_mode(tmp_path: Path) -> Iterator[Path]:
    from spark import files as files_pkg
    from spark import mode
    from spark.mode import DatabaseSettings, Dialect, Mode, RuntimeSettings

    volume = tmp_path / "server-volume"
    mode.configure(
        RuntimeSettings(Mode.SERVER, DatabaseSettings(Dialect.POSTGRESQL, url="postgresql://x"))
    )
    files_pkg.configure(
        _Settings({"files.root": str(volume)}), data_dir=tmp_path, runtime_mode=Mode.SERVER
    )
    try:
        yield volume
    finally:
        files_pkg.reset()
        mode.reset()


class TestRegistry:
    def test_server_mode_offers_document_and_archive_tools_without_allowed_paths(
        self, server_mode: Path
    ) -> None:
        from spark.tools.registry import get_builtin_tools

        names = {t["name"] for t in get_builtin_tools({"embedded_tools": {}})}
        assert {"read_word", "list_archive"} <= names

    def test_server_mode_refuses_an_escape_as_an_error(
        self, server_mode: Path, bridge: Any
    ) -> None:
        from spark.tools.registry import execute_builtin_tool

        config = {"embedded_tools": {}, "_user_guid": ALICE, "_files_db": bridge}
        text, is_error = execute_builtin_tool("read_word", {"path": "../x.docx"}, config)
        assert is_error is True
        assert text.startswith("Path refused")

    def test_server_mode_reads_from_the_users_area(self, server_mode: Path, bridge: Any) -> None:
        from spark import files as files_pkg
        from spark.tools.registry import execute_builtin_tool

        _docx(files_pkg.file_store().local_path(ALICE, "u/a.docx"), "From the area")
        config = {"embedded_tools": {}, "_kg_user_guid": ALICE, "_kg_db": bridge}
        text, is_error = execute_builtin_tool("read_word", {"path": "u/a.docx"}, config)
        assert (text, is_error) == ("From the area", False)

    def test_server_mode_creates_in_the_users_area(self, server_mode: Path, bridge: Any) -> None:
        from spark.tools.registry import execute_builtin_tool

        config = {
            "embedded_tools": {"documents": {"mode": "read_write"}},
            "_user_guid": ALICE,
            "_files_db": bridge,
        }
        text, is_error = execute_builtin_tool(
            "create_pdf", {"path": "out.pdf", **_CREATE_INPUTS["create_pdf"]}, config
        )
        assert is_error is False, text
        assert text == "PDF document created: out.pdf"
        assert bridge.call(files_db.get_user_file_by_key, ALICE, "out.pdf") is not None

    def test_server_mode_without_a_user_is_an_error(self, server_mode: Path) -> None:
        from spark.tools.registry import execute_builtin_tool

        text, is_error = execute_builtin_tool("list_archive", {"path": "a.zip"}, {})
        assert is_error is True
        assert "signed-in user" in text

    def test_personal_mode_refuses_outside_the_roots_as_an_error(self, tmp_path: Path) -> None:
        from spark.tools.registry import execute_builtin_tool

        root = tmp_path / "allowed"
        root.mkdir()
        config = {"embedded_tools": {"filesystem": {"allowed_paths": [str(root)]}}}
        text, is_error = execute_builtin_tool(
            "read_pdf", {"path": str(tmp_path / "outside.pdf")}, config
        )
        assert is_error is True
        assert text.startswith("Path refused")

    def test_personal_mode_without_roots_is_an_error(self) -> None:
        from spark.tools.registry import execute_builtin_tool

        text, is_error = execute_builtin_tool("read_pdf", {"path": "a.pdf"}, {"embedded_tools": {}})
        assert is_error is True
        assert "allowed_paths" in text


class TestCallSitesPassTheUser:
    """Sub-agents and scheduled actions give file tools the user and the bridge."""

    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        from spark.tools import registry

        seen: dict[str, Any] = {}

        def fake_execute(name: str, tool_input: dict, config: dict) -> tuple[str, bool]:
            seen.update(config)
            return "ok", False

        monkeypatch.setattr(registry, "execute_builtin_tool", fake_execute)
        monkeypatch.setattr(registry, "get_builtin_tools", lambda config: [{"name": "read_word"}])
        return seen

    def test_agent_executor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from spark.core.agent_executor import AgentExecutor

        seen = self._capture(monkeypatch)
        db = MagicMock()
        ex = AgentExecutor(MagicMock(), db, {"embedded_tools": {}}, user_guid=ALICE)
        ex._execute_tool("read_word", {"path": "a.docx"})
        assert seen["_user_guid"] == ALICE
        assert seen["_files_db"] is db

    def test_action_executor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import MagicMock

        from spark.scheduler.executor import ActionExecutor

        seen = self._capture(monkeypatch)
        monkeypatch.setattr("spark.core.user_guid.get_user_guid", lambda ctx=None: BOB)
        ctx = MagicMock()
        ctx.settings.get.return_value = {}
        db = MagicMock()
        ex = ActionExecutor(ctx, "daemon-1", db)
        ex._execute_tool("read_word", {"path": "a.docx"})
        assert seen["_user_guid"] == BOB
        assert seen["_files_db"] is db
