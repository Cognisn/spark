"""Chat attachments: upload to the user's area, name the files in the message, read by tool.

The composer uploads each file to ``/files/api`` with the conversation id (the
CSRF-wrapped fetch), then sends the message with the file ids. The send routes
check each id is the signed-in user's own file and add a note naming the files
by name and id, never their content; the model reads them with read_my_file.

Guards under test:

* an attachment is visible only to its owner: another user's file id answers
  404 on both send routes, and the turn never runs;
* the upload needs the session's CSRF token;
* the note carries names and ids only, never content;
* end to end through the app client, the model reads the attached file with
  read_my_file, and the conversation stores the note, not the content.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from spark import files as files_pkg
from spark.database import conversations as convdb
from spark.mode import Mode
from spark.web import attachments
from spark.web.security import CSRF_HEADER
from spark.web.server import attach_database, create_app
from tests.test_conversation_manager import StubLLMService
from tests.web_helpers import sign_in

ALICE = "user-alice-0001"
BOB = "user-bob-0002"
CONTENT = b"synthetic budget figures for example.org: 42 widgets"
NOTE_LINE = re.compile(r"^- file (\d+): (.+)$", re.M)


class _Settings:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = values

    def get(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.values.get(key, default)


def _mock_settings_get(key: str, default: object = None, *, cast: type | None = None) -> object:
    return {"interface.session_timeout_minutes": 60}.get(key, default)


@pytest.fixture
def file_settings(tmp_path: Path) -> Iterator[_Settings]:
    settings = _Settings({"files.root": str(tmp_path / "volume")})
    files_pkg.configure(settings, data_dir=tmp_path, runtime_mode=Mode.SERVER)
    yield settings
    files_pkg.reset()


@pytest.fixture
def client(spark_db_settings: Any, file_settings: _Settings) -> Iterator[TestClient]:
    ctx = MagicMock()
    ctx.settings.get = _mock_settings_get
    app = create_app(ctx, first_run=False)
    attach_database(app, spark_db_settings, True)
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    with TestClient(app, backend_options=options) as client:
        app.state.user_guid = ALICE
        sign_in(client)
        yield client


class RecordingManager:
    """Stands in for the ConversationManager: records each turn it is asked to run."""

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def send_message(
        self, conversation_id: int, message: str, user_guid: str, **kwargs: Any
    ) -> dict[str, Any]:
        self.turns.append(
            {"conversation_id": conversation_id, "message": message, "user": user_guid, **kwargs}
        )
        return {
            "content": "ok",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "tool_calls": [],
            "iterations": 0,
            "status": "completed",
        }


@pytest.fixture
def manager(client: TestClient) -> RecordingManager:
    recorder = RecordingManager()
    client.app.state.conversation_manager = recorder  # type: ignore[attr-defined]
    return recorder


def _as(client: TestClient, user: str) -> None:
    client.app.state.user_guid = user  # type: ignore[attr-defined]


def _conversation(client: TestClient, user: str) -> int:
    cid: int = client.app.state.sync_db.call(  # type: ignore[attr-defined]
        convdb.create_conversation, "Synthetic", "stub-model", user
    )
    return cid


def _upload(client: TestClient, name: str, data: bytes, cid: int | None = None) -> int:
    form = {"conversation_id": str(cid)} if cid is not None else {}
    resp = client.post("/files/api", files={"file": (name, data, "text/plain")}, data=form)
    assert resp.status_code == 201, resp.text
    file_id: int = resp.json()["file"]["id"]
    return file_id


def _stream(client: TestClient, cid: int, message: str, ids: str | None) -> Any:
    query = f"/stream/chat?conversation_id={cid}&message={message}"
    if ids is not None:
        query += f"&attachments={ids}"
    return client.get(query, headers={"Accept": "text/event-stream"})


# -- The note -------------------------------------------------------------------------------


class TestNote:
    def test_names_each_file_by_id_after_the_message(self) -> None:
        rows = [{"id": 3, "filename": "a.txt"}, {"id": 5, "filename": "b.pdf"}]
        assert attachments.with_attachments("Please summarise.", rows) == (
            "Please summarise.\n\n"
            "[Attached files: read them with the read_my_file tool]\n"
            "- file 3: a.txt\n"
            "- file 5: b.pdf"
        )

    def test_a_message_of_attachments_alone_is_only_the_note(self) -> None:
        assert attachments.with_attachments("", [{"id": 7, "filename": "c.txt"}]) == (
            "[Attached files: read them with the read_my_file tool]\n- file 7: c.txt"
        )

    def test_no_attachments_leaves_the_message_alone(self) -> None:
        assert attachments.with_attachments("hello", []) == "hello"

    def test_a_name_cannot_add_lines_to_the_note(self) -> None:
        note = attachments.with_attachments("x", [{"id": 1, "filename": "a\nb\r- file 9: c"}])
        assert NOTE_LINE.findall(note) == [("1", "a b - file 9: c")]


class TestIds:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (None, []),
            ("", []),
            ("3", [3]),
            ("3,5", [3, 5]),
            (" 3 , 5 ,3", [3, 5]),
            ([4, "6"], [4, 6]),
        ],
    )
    def test_accepts(self, raw: Any, expected: list[int]) -> None:
        assert attachments.parse_ids(raw) == expected

    @pytest.mark.parametrize("raw", ["x", "1,,2", "-1", "1.5", [True], [None], {"a": 1}, 7])
    def test_refuses(self, raw: Any) -> None:
        with pytest.raises(attachments.AttachmentError):
            attachments.parse_ids(raw)

    def test_refuses_more_than_the_limit(self) -> None:
        many = ",".join(str(i) for i in range(attachments.MAX_ATTACHMENTS + 1))
        with pytest.raises(attachments.AttachmentError):
            attachments.parse_ids(many)


# -- The send routes ------------------------------------------------------------------------


class TestStreamRoute:
    def test_names_the_users_own_files_in_the_turns_message(
        self, client: TestClient, manager: RecordingManager
    ) -> None:
        cid = _conversation(client, ALICE)
        fid = _upload(client, "budget.txt", CONTENT, cid)

        resp = _stream(client, cid, "Summarise this", str(fid))

        assert resp.status_code == 200, resp.text
        (turn,) = manager.turns
        assert turn["message"].startswith("Summarise this\n\n[Attached files:")
        assert f"- file {fid}: budget.txt" in turn["message"]
        assert CONTENT.decode() not in turn["message"]

    def test_another_users_file_is_not_found_and_the_turn_never_runs(
        self, client: TestClient, manager: RecordingManager
    ) -> None:
        _as(client, BOB)
        theirs = _upload(client, "bob.txt", b"bravo")
        _as(client, ALICE)
        cid = _conversation(client, ALICE)
        mine = _upload(client, "alice.txt", b"alpha", cid)

        resp = _stream(client, cid, "x", f"{mine},{theirs}")

        assert resp.status_code == 404
        assert manager.turns == []

    def test_unusable_ids_are_refused(self, client: TestClient, manager: RecordingManager) -> None:
        cid = _conversation(client, ALICE)
        resp = _stream(client, cid, "x", "abc")
        assert resp.status_code == 400
        assert manager.turns == []

    def test_without_attachments_the_message_is_unchanged(
        self, client: TestClient, manager: RecordingManager
    ) -> None:
        cid = _conversation(client, ALICE)
        _stream(client, cid, "plain", None)
        assert manager.turns[0]["message"] == "plain"


class TestSendRoute:
    def test_names_the_files_and_passes_the_users_permissions(
        self, client: TestClient, manager: RecordingManager
    ) -> None:
        cid = _conversation(client, ALICE)
        fid = _upload(client, "budget.txt", CONTENT, cid)

        resp = client.post(
            f"/chat/{cid}/api/send", json={"message": "Read it", "attachments": [fid]}
        )

        assert resp.status_code == 200, resp.text
        (turn,) = manager.turns
        assert f"- file {fid}: budget.txt" in turn["message"]
        assert CONTENT.decode() not in turn["message"]
        assert "permissions" in turn

    def test_attachments_alone_make_a_message(
        self, client: TestClient, manager: RecordingManager
    ) -> None:
        cid = _conversation(client, ALICE)
        fid = _upload(client, "budget.txt", CONTENT, cid)
        resp = client.post(f"/chat/{cid}/api/send", json={"message": "", "attachments": [fid]})
        assert resp.status_code == 200
        assert manager.turns[0]["message"].startswith("[Attached files:")

    def test_an_empty_message_without_attachments_is_still_refused(
        self, client: TestClient, manager: RecordingManager
    ) -> None:
        cid = _conversation(client, ALICE)
        resp = client.post(f"/chat/{cid}/api/send", json={"message": " "})
        assert resp.status_code == 400

    def test_another_users_file_is_not_found(
        self, client: TestClient, manager: RecordingManager
    ) -> None:
        _as(client, BOB)
        theirs = _upload(client, "bob.txt", b"bravo")
        _as(client, ALICE)
        cid = _conversation(client, ALICE)

        resp = client.post(f"/chat/{cid}/api/send", json={"message": "x", "attachments": [theirs]})

        assert resp.status_code == 404
        assert manager.turns == []

    def test_unusable_ids_are_refused(self, client: TestClient, manager: RecordingManager) -> None:
        cid = _conversation(client, ALICE)
        resp = client.post(f"/chat/{cid}/api/send", json={"message": "x", "attachments": "1;2"})
        assert resp.status_code == 400
        assert manager.turns == []


class TestUploadNeedsTheCsrfToken:
    @pytest.mark.parametrize("token", ["", "not-the-sessions-token"])
    def test_refused_without_the_sessions_token(self, client: TestClient, token: str) -> None:
        cid = _conversation(client, ALICE)
        resp = client.post(
            "/files/api",
            files={"file": ("a.txt", b"alpha", "text/plain")},
            data={"conversation_id": str(cid)},
            headers={CSRF_HEADER: token},
        )
        assert resp.status_code == 403
        listing = client.get("/files/api").json()
        assert listing["files"] == []


# -- End to end -----------------------------------------------------------------------------


class ReadsAttachments(StubLLMService):
    """A model that reads every attached file it is told of, then answers with what it read."""

    def __init__(self) -> None:
        super().__init__()
        self._seen["tools"] = []

    def invoke_model(self, messages: list[dict], **kwargs: Any) -> dict:
        self._seen["tools"].append([t.get("name") for t in kwargs.get("tools") or []])
        last = messages[-1]["content"]
        if isinstance(last, str) and "[Attached files:" in last:
            calls = [
                {"type": "tool_use", "id": f"read-{fid}", "name": "read_my_file",
                 "input": {"file_id": int(fid)}}
                for fid, _ in NOTE_LINE.findall(last)
            ]  # fmt: skip
            return {
                "content": "",
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "tool_use": calls,
                "content_blocks": calls,
            }
        return {
            "content": "read",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "tool_use": None,
            "content_blocks": [{"type": "text", "text": "read"}],
        }


def _real_manager(client: TestClient) -> ReadsAttachments:
    from spark.core.conversation_manager import ConversationManager
    from spark.llm.context_limits import ContextLimitResolver
    from spark.llm.manager import LLMManager

    stub = ReadsAttachments()
    llm = LLMManager()
    llm.register_provider(stub)  # type: ignore[arg-type]
    mgr = ConversationManager(
        client.app.state.sync_db,  # type: ignore[attr-defined]
        llm,
        ContextLimitResolver(),
        max_tool_iterations=4,
        user_guid=ALICE,
        embedded_tools_config={"embedded_tools": {}},
    )
    mgr._get_llm_service_for_model = lambda model_id: stub  # type: ignore[method-assign]
    # The vector index is not under test here (and would load an embedding model).
    mgr._get_vector_index = lambda conversation_id, user_guid: None  # type: ignore[method-assign]
    client.app.state.conversation_manager = mgr  # type: ignore[attr-defined]
    return stub


def _always_allow(client: TestClient, user: str) -> None:
    """The user's earlier "Always allow" for the documents tools, so the stream never waits."""
    from spark.database import tool_permissions

    for name in ("read_my_file", "list_my_files"):
        client.app.state.sync_db.call(  # type: ignore[attr-defined]
            tool_permissions.set_global_tool_permission, user, name, "allowed"
        )


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        event = re.search(r"^event: (.+)$", block, re.M)
        data = re.search(r"^data: (.+)$", block, re.M)
        if event and data:
            try:
                found.append((event.group(1), json.loads(data.group(1))))
            except ValueError:
                continue
    return found


class TestEndToEnd:
    def test_the_model_reads_an_attached_file_and_the_note_is_what_is_stored(
        self, client: TestClient
    ) -> None:
        stub = _real_manager(client)
        _always_allow(client, ALICE)
        cid = _conversation(client, ALICE)
        fid = _upload(client, "budget.txt", CONTENT, cid)

        resp = _stream(client, cid, "Summarise the budget", str(fid))

        assert resp.status_code == 200, resp.text
        completes = [d for e, d in _events(resp.text) if e == "tool_complete"]
        assert [d["tool_name"] for d in completes] == ["read_my_file"]
        assert CONTENT.decode() in completes[0]["result"]
        assert "read_my_file" in stub._seen["tools"][0]

        history = client.get(f"/chat/{cid}/api/history").json()
        user_messages = [m["content"] for m in history if m["role"] == "user"]
        assert any(f"- file {fid}: budget.txt" in m for m in user_messages)
        assert not any(CONTENT.decode() in m for m in user_messages if "[Attached files:" in m)

    def test_a_turn_cannot_read_another_users_file_by_guessing_its_id(
        self, client: TestClient
    ) -> None:
        _as(client, BOB)
        theirs = _upload(client, "bob.txt", b"bravo private figures")
        _as(client, ALICE)
        _real_manager(client)
        _always_allow(client, ALICE)
        cid = _conversation(client, ALICE)
        mgr = client.app.state.conversation_manager  # type: ignore[attr-defined]

        # As if the model had invented the id: the tool still acts for the turn's user.
        text, is_error = mgr._call_tool(
            "read_my_file", {"file_id": theirs}, conversation_id=cid, user_guid=ALICE
        )

        assert is_error
        assert "bravo" not in text
