"""The document and archive tools find the signed-in user's own file by its bare name.

A chat upload is stored under ``uploads/<id>/<name>`` in the user's area, so a
model that calls read_pdf with the attachment's bare name ("Interview
Criteria (1).pdf") used to get "File not found". In server mode a path with no
directory part that does not resolve is now looked up by file name among the
turn's user's own files, through the database layer for that user only. Guards
under test:

* one match is read, with a first line naming its file id, name and area key
  (never the volume path); exact names before case-insensitive ones, and this
  conversation's files before others;
* several matches are listed by file id, name, size and upload time and none is
  read; no match lists up to five of the user's own most recent files;
* another user's file of the same name is never read, named or counted, and a
  file on disk with no record (or in another user's area) is never found;
* a path with a directory part is never resolved by name, and ``..`` is still
  refused by containment; keys that resolve behave exactly as before;
* personal mode is unchanged.
"""

from __future__ import annotations

import io
import tarfile
import uuid
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from spark import files as files_pkg
from spark.identity.permissions import USER, role_permissions
from spark.mode import Mode
from spark.tools import documents
from spark.tools.registry import PERMISSIONS_KEY, execute_builtin_tool

ALICE = "user-alice-0001"
BOB = "user-bob-0002"
CAROL = "user-carol-0003"
INTERVIEW = "Interview Criteria (1).pdf"


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
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Server mode with a file volume."""
    from spark import mode

    monkeypatch.setattr(mode, "current_mode", lambda: Mode.SERVER)
    root = tmp_path / "volume"
    files_pkg.configure(
        _Settings({"files.root": str(root)}), data_dir=tmp_path, runtime_mode=Mode.SERVER
    )
    yield root
    files_pkg.reset()


# -- Synthetic documents ----------------------------------------------------------------------


def pdf_bytes(text: str) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    page.drawString(72, 720, text)
    page.save()
    return buffer.getvalue()


def docx_bytes(text: str) -> bytes:
    from docx import Document

    buffer = io.BytesIO()
    doc = Document()
    doc.add_paragraph(text)
    doc.save(buffer)
    return buffer.getvalue()


def xlsx_bytes(text: str) -> bytes:
    from openpyxl import Workbook

    buffer = io.BytesIO()
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet["A1"] = text
    book.save(buffer)
    return buffer.getvalue()


def pptx_bytes(text: str) -> bytes:
    from pptx import Presentation

    buffer = io.BytesIO()
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    assert slide.shapes.title is not None
    slide.shapes.title.text = text
    deck.save(buffer)
    return buffer.getvalue()


def zip_bytes(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("inside/notes.txt", text)
    return buffer.getvalue()


def tar_bytes(text: str) -> bytes:
    buffer = io.BytesIO()
    data = text.encode()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo("inside/notes.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# tool -> (file name, builder, what the result shows of the text)
READERS: dict[str, tuple[str, Callable[[str], bytes]]] = {
    "read_pdf": (INTERVIEW, pdf_bytes),
    "read_word": ("Interview Notes.docx", docx_bytes),
    "read_excel": ("Interview Scores.xlsx", xlsx_bytes),
    "read_powerpoint": ("Interview Deck.pptx", pptx_bytes),
}


def upload(bridge: Any, user: str, name: str, data: bytes, cid: int | None = None) -> int:
    """Store a file as the upload endpoint does: ``uploads/<hex>/<name>``."""
    return files_pkg.file_area(bridge).save(
        user,
        f"uploads/{uuid.uuid4().hex}/{name}",
        io.BytesIO(data),
        filename=name,
        conversation_id=cid,
    )


def key_of(bridge: Any, user: str, file_id: int) -> str:
    from spark.database import files as files_db

    row = bridge.call(files_db.get_user_file, user, file_id)
    assert row is not None
    return str(row["storage_key"])


def config(bridge: Any, user: str, *, cid: int | None = None, **embedded: Any) -> dict[str, Any]:
    """The configuration a turn's tool call carries (as ConversationManager._call_tool builds it)."""
    return {
        "embedded_tools": embedded,
        "_user_guid": user,
        "_kg_db": bridge,
        "_kg_conversation_id": cid,
        "_kg_user_guid": user,
        PERMISSIONS_KEY: role_permissions(USER),
    }


def run(tool: str, args: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, bool]:
    return execute_builtin_tool(tool, args, cfg)


# -- One match ----------------------------------------------------------------------------------


class TestOneMatch:
    @pytest.mark.parametrize("tool", sorted(READERS))
    def test_a_bare_name_reads_the_users_upload(self, bridge: Any, volume: Path, tool: str) -> None:
        name, build = READERS[tool]
        secret = f"Synthetic {tool} content for Example Pty Ltd"
        fid = upload(bridge, ALICE, name, build(secret))
        key = key_of(bridge, ALICE, fid)

        text, is_error = run(tool, {"path": name}, config(bridge, ALICE))

        assert not is_error, text
        first, _, rest = text.partition("\n")
        assert first == f"Read file {fid}: {name} ({key})"
        assert secret in rest
        assert str(volume) not in text and str(volume.resolve()) not in text

    def test_the_interview_pdf_from_the_test_instance(self, bridge: Any, volume: Path) -> None:
        fid = upload(bridge, ALICE, INTERVIEW, pdf_bytes("Synthetic criteria: clarity"))

        text, is_error = run("read_pdf", {"path": INTERVIEW}, config(bridge, ALICE))

        assert not is_error
        assert text.startswith(f"Read file {fid}: {INTERVIEW} (uploads/")
        assert "Synthetic criteria: clarity" in text
        assert "File not found" not in text

    def test_a_case_insensitive_name_is_found_when_there_is_no_exact_one(
        self, bridge: Any, volume: Path
    ) -> None:
        fid = upload(bridge, ALICE, INTERVIEW, pdf_bytes("Synthetic folded"))

        text, _ = run("read_pdf", {"path": "interview criteria (1).PDF"}, config(bridge, ALICE))

        assert text.startswith(f"Read file {fid}: {INTERVIEW} (")
        assert "Synthetic folded" in text

    def test_an_exact_name_wins_over_a_case_insensitive_one(
        self, bridge: Any, volume: Path
    ) -> None:
        upload(bridge, ALICE, "Report.pdf", pdf_bytes("Synthetic upper"))
        exact = upload(bridge, ALICE, "report.pdf", pdf_bytes("Synthetic lower"))

        text, _ = run("read_pdf", {"path": "report.pdf"}, config(bridge, ALICE))

        assert text.startswith(f"Read file {exact}: report.pdf (")
        assert "Synthetic lower" in text and "Synthetic upper" not in text

    def test_this_conversations_attachment_wins_over_an_older_one(
        self, bridge: Any, volume: Path
    ) -> None:
        upload(bridge, ALICE, "notes.docx", docx_bytes("Synthetic older notes"), cid=1)
        here = upload(bridge, ALICE, "notes.docx", docx_bytes("Synthetic current notes"), cid=2)

        text, _ = run("read_word", {"path": "notes.docx"}, config(bridge, ALICE, cid=2))

        assert text.startswith(f"Read file {here}: notes.docx (")
        assert "Synthetic current notes" in text and "older" not in text

    @pytest.mark.parametrize(
        ("name", "build"), [("bundle.zip", zip_bytes), ("bundle.tar", tar_bytes)]
    )
    def test_list_archive_by_bare_name(
        self, bridge: Any, volume: Path, name: str, build: Callable[[str], bytes]
    ) -> None:
        fid = upload(bridge, ALICE, name, build("synthetic"))

        text, is_error = run("list_archive", {"path": name}, config(bridge, ALICE))

        assert not is_error, text
        assert text.startswith(f"Read file {fid}: {name} (uploads/")
        assert "inside/notes.txt" in text

    def test_extract_archive_by_bare_name(self, bridge: Any, volume: Path) -> None:
        fid = upload(bridge, ALICE, "bundle.zip", zip_bytes("synthetic extracted words"))

        text, is_error = run(
            "extract_archive",
            {"path": "bundle.zip", "destination": "unpacked"},
            config(bridge, ALICE, archives={"mode": "extract"}),
        )

        assert not is_error, text
        assert text.startswith(f"Read file {fid}: bundle.zip (uploads/")
        assert "Extracted 1 files to unpacked" in text
        stored = files_pkg.file_store().local_path(ALICE, "unpacked/inside/notes.txt")
        assert stored.read_text() == "synthetic extracted words"

    def test_a_key_that_resolves_reads_exactly_as_before(self, bridge: Any, volume: Path) -> None:
        fid = upload(bridge, ALICE, INTERVIEW, pdf_bytes("Synthetic by key"))
        key = key_of(bridge, ALICE, fid)
        scope_text = documents._read_pdf(files_pkg.file_store().local_path(ALICE, key), 100)

        text, is_error = run("read_pdf", {"path": key}, config(bridge, ALICE))

        assert not is_error
        assert text == scope_text


# -- Several matches and none ------------------------------------------------------------------


class TestSeveralOrNone:
    def test_several_matches_are_listed_and_none_is_read(self, bridge: Any, volume: Path) -> None:
        first = upload(bridge, ALICE, INTERVIEW, pdf_bytes("Synthetic first draft"))
        second = upload(bridge, ALICE, INTERVIEW, pdf_bytes("Synthetic second draft"))

        text, _ = run("read_pdf", {"path": INTERVIEW}, config(bridge, ALICE))

        assert "Synthetic" not in text
        assert "Read file" not in text
        for fid in (first, second):
            assert f"file {fid}: {INTERVIEW}" in text
        assert "bytes" in text and "uploaded" in text
        assert "read_my_file" in text

    def test_several_in_this_conversation_are_listed_too(self, bridge: Any, volume: Path) -> None:
        upload(bridge, ALICE, "a.pdf", pdf_bytes("Synthetic one"), cid=4)
        upload(bridge, ALICE, "a.pdf", pdf_bytes("Synthetic two"), cid=4)
        upload(bridge, ALICE, "a.pdf", pdf_bytes("Synthetic three"), cid=5)

        text, _ = run("read_pdf", {"path": "a.pdf"}, config(bridge, ALICE, cid=4))

        assert "Synthetic" not in text
        assert text.count("- file ") == 2

    def test_no_match_lists_the_five_most_recent_of_the_users_own_files(
        self, bridge: Any, volume: Path
    ) -> None:
        ids = [upload(bridge, ALICE, f"synthetic-{n}.txt", b"words") for n in range(7)]
        upload(bridge, BOB, "bob-private.txt", b"bob words")

        text, is_error = run("read_pdf", {"path": "missing.pdf"}, config(bridge, ALICE))

        assert "not in your file area" in text
        assert "missing.pdf" in text
        for fid in ids[2:]:
            assert f"file {fid}: synthetic-" in text
        for fid in ids[:2]:
            assert f"file {fid}:" not in text
        assert "bob" not in text.lower()
        assert "read_my_file" in text and "list_my_files" in text

    def test_no_match_without_files_says_so(self, bridge: Any, volume: Path) -> None:
        upload(bridge, BOB, "missing.pdf", pdf_bytes("Synthetic bob"))

        text, _ = run("read_pdf", {"path": "missing.pdf"}, config(bridge, CAROL))

        assert "not in your file area" in text
        assert "- file" not in text
        assert "Synthetic" not in text


# -- Per-user isolation ------------------------------------------------------------------------


class TestIsolation:
    def test_each_user_reads_only_their_own_file_of_the_same_name(
        self, bridge: Any, volume: Path
    ) -> None:
        alices = upload(bridge, ALICE, INTERVIEW, pdf_bytes("Synthetic alice criteria"))
        bobs = upload(bridge, BOB, INTERVIEW, pdf_bytes("Synthetic bob criteria"))

        alice_text, _ = run("read_pdf", {"path": INTERVIEW}, config(bridge, ALICE))
        bob_text, _ = run("read_pdf", {"path": INTERVIEW}, config(bridge, BOB))

        # One match each: the other user's file is never counted as a second match.
        assert alice_text.startswith(f"Read file {alices}: ")
        assert "Synthetic alice criteria" in alice_text
        assert "bob" not in alice_text.lower() and f"file {bobs}" not in alice_text
        assert bob_text.startswith(f"Read file {bobs}: ")
        assert "Synthetic bob criteria" in bob_text
        assert "alice" not in bob_text.lower() and f"file {alices}" not in bob_text

    def test_another_users_file_is_never_found_by_name(self, bridge: Any, volume: Path) -> None:
        bobs = upload(bridge, BOB, INTERVIEW, pdf_bytes("Synthetic bob only"))

        text, _ = run("read_pdf", {"path": INTERVIEW}, config(bridge, ALICE))

        assert "Synthetic bob only" not in text
        assert f"file {bobs}" not in text
        assert "not in your file area" in text

    def test_a_file_on_disk_without_a_record_is_never_found(
        self, bridge: Any, volume: Path
    ) -> None:
        store = files_pkg.file_store()
        for user in (ALICE, BOB):
            stray = store.local_path(user, f"uploads/{uuid.uuid4().hex}/stray.pdf")
            stray.parent.mkdir(parents=True, exist_ok=True)
            stray.write_bytes(pdf_bytes("Synthetic stray"))

        text, _ = run("read_pdf", {"path": "stray.pdf"}, config(bridge, ALICE))

        assert "Synthetic stray" not in text
        assert "not in your file area" in text

    def test_the_lookup_is_for_the_scopes_user(self, bridge: Any, volume: Path) -> None:
        upload(bridge, BOB, INTERVIEW, pdf_bytes("Synthetic bob scoped"))
        cfg = config(bridge, ALICE)
        cfg["_kg_user_guid"] = BOB  # the turn's user wins, as for every file tool

        text, _ = run("read_pdf", {"path": INTERVIEW}, cfg)

        assert "Synthetic bob scoped" not in text


# -- Paths that are not bare names -----------------------------------------------------------


class TestNotBareNames:
    @pytest.mark.parametrize(
        "path",
        [
            f"other/{INTERVIEW}",
            f"uploads/{INTERVIEW}",
            f"uploads\\{INTERVIEW}",
            f"./sub/{INTERVIEW}",
        ],
    )
    def test_a_path_with_a_directory_part_is_not_resolved_by_name(
        self, bridge: Any, volume: Path, path: str
    ) -> None:
        upload(bridge, ALICE, INTERVIEW, pdf_bytes("Synthetic by name only"))

        text, _ = run("read_pdf", {"path": path}, config(bridge, ALICE))

        assert "Synthetic by name only" not in text
        assert "Read file" not in text
        assert text.startswith("File not found")

    @pytest.mark.parametrize(
        "path", [f"../{INTERVIEW}", f"uploads/../../{INTERVIEW}", "..", "/etc/x.pdf"]
    )
    def test_escapes_are_still_refused_by_containment(
        self, bridge: Any, volume: Path, path: str
    ) -> None:
        upload(bridge, ALICE, INTERVIEW, pdf_bytes("Synthetic contained"))

        text, is_error = run("read_pdf", {"path": path}, config(bridge, ALICE))

        assert is_error
        assert text.startswith("Path refused")
        assert "Synthetic contained" not in text


# -- What the tools say -------------------------------------------------------------------------


PATH_TOOLS = (
    "read_pdf",
    "read_word",
    "read_excel",
    "read_powerpoint",
    "list_archive",
    "extract_archive",
)


class TestDescriptions:
    @pytest.fixture
    def definitions(self) -> dict[str, dict[str, Any]]:
        from spark.tools import archives

        tools = documents.get_tools() + archives.get_tools(mode="extract")
        return {t["name"]: t for t in tools}

    @pytest.mark.parametrize("tool", PATH_TOOLS)
    def test_each_path_tool_points_attachments_at_read_my_file(
        self, definitions: dict[str, dict[str, Any]], tool: str
    ) -> None:
        definition = definitions[tool]
        assert "read_my_file" in definition["description"]
        assert "file id" in definition["description"]
        path = definition["inputSchema"]["properties"]["path"]["description"]
        assert "server mode" in path and "list_my_files" in path and "key" in path
        assert len(definition["description"]) < 160

    @pytest.mark.parametrize("tool", PATH_TOOLS)
    def test_each_tool_document_says_the_same(self, tool: str) -> None:
        from spark.tools.registry import _get_tool_documentation

        text = _get_tool_documentation(tool)
        assert "read_my_file" in text and "list_my_files" in text
        assert "server mode" in text.lower()

    def test_the_index_ties_attachments_to_read_my_file(self) -> None:
        from spark.tools.registry import _get_tool_documentation

        index = _get_tool_documentation("_index")
        assert "attach" in index.lower() and "read_my_file" in index

    def test_list_my_files_shows_the_key_the_path_tools_take(
        self, bridge: Any, volume: Path
    ) -> None:
        fid = upload(bridge, ALICE, INTERVIEW, pdf_bytes("Synthetic"))
        key = key_of(bridge, ALICE, fid)

        text, _ = run("list_my_files", {}, config(bridge, ALICE))

        assert f"file {fid}: {INTERVIEW}" in text and f"key {key}" in text
        listed, _ = run("read_pdf", {"path": key}, config(bridge, ALICE))
        assert "Synthetic" in listed


# -- Personal mode -----------------------------------------------------------------------------


class TestPersonalMode:
    @pytest.fixture
    def allowed(self, tmp_path: Path) -> Iterator[Path]:
        root = tmp_path / "allowed"
        root.mkdir()
        files_pkg.configure(_Settings({}), data_dir=tmp_path, runtime_mode=Mode.PERSONAL)
        yield root
        files_pkg.reset()

    def _cfg(self, bridge: Any, root: Path) -> dict[str, Any]:
        cfg = config(bridge, ALICE)
        cfg["embedded_tools"] = {"filesystem": {"allowed_paths": [str(root)]}}
        return cfg

    def test_a_missing_name_is_not_found_as_before(self, bridge: Any, allowed: Path) -> None:
        upload(bridge, ALICE, INTERVIEW, pdf_bytes("Synthetic personal upload"))

        text, is_error = run("read_pdf", {"path": INTERVIEW}, self._cfg(bridge, allowed))

        assert not is_error
        assert text == f"File not found: {(allowed / INTERVIEW).resolve()}"

    def test_a_file_in_the_allowed_root_reads_as_before(self, bridge: Any, allowed: Path) -> None:
        (allowed / INTERVIEW).write_bytes(pdf_bytes("Synthetic allowed root"))

        text, is_error = run("read_pdf", {"path": INTERVIEW}, self._cfg(bridge, allowed))

        assert not is_error
        assert text.startswith("--- Page 1 ---")
        assert "Synthetic allowed root" in text
