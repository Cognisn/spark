"""The user bundle format: writing, and every guard on reading one (A15, A17).

Bundles here are built in memory from synthetic rows; hostile archives are
built by hand with :mod:`zipfile`.
"""

from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from typing import Any

import pytest

from spark.bundles import format as bf
from spark.bundles.reader import BundleRefused, Limits, open_bundle


def _bundle(**extra: Any) -> bytes:
    buf = io.BytesIO()
    with bf.BundleWriter(buf, spark_version="0.3.0a10") as writer:
        writer.add_rows(
            "conversations",
            [{"id": 1, "name": "Synthetic chat", "model_id": "model-a", "user_guid": "u-1"}],
        )
        writer.add_rows("messages", [{"id": 7, "conversation_id": 1, "content": "hello"}])
        sha = writer.add_blob(b"%PDF-1.4 synthetic")
        writer.add_rows("user_files", [{"id": 3, "storage_key": "uploads/a/n.pdf", "blob": sha}])
        writer.add_skill_file("note-taker", "SKILL.md", b"---\nname: note-taker\n---\nbody\n")
        writer.add_skill_file("note-taker", "templates/a.txt", b"template")
        writer.set_preferences({"interface.theme": "light"})
        for key, value in extra.items():
            writer.set_meta(key, value)
    return buf.getvalue()


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _rebuild(
    members: dict[str, bytes],
    *,
    manifest: dict[str, Any] | None = None,
    fix_hashes: bool = True,
    extra_infos: list[tuple[zipfile.ZipInfo, bytes]] | None = None,
) -> bytes:
    """A zip of ``members``; the manifest's member table is recomputed unless told not to."""
    members = dict(members)
    doc = manifest if manifest is not None else json.loads(members[bf.MANIFEST])
    if fix_hashes:
        doc["members"] = {
            name: {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
            for name, body in members.items()
            if name != bf.MANIFEST
        }
    members[bf.MANIFEST] = json.dumps(doc).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in members.items():
            zf.writestr(name, body)
        for info, body in extra_infos or []:
            zf.writestr(info, body)
    return buf.getvalue()


def _open(data: bytes, limits: Limits | None = None) -> Any:
    return open_bundle(io.BytesIO(data), limits or Limits())


# -- Writing and reading back -------------------------------------------------------


def test_round_trip_rows_blobs_skills_and_preferences() -> None:
    with _open(_bundle()) as bundle:
        assert bundle.manifest["format"] == "spark-user-bundle"
        assert bundle.manifest["format_version"] == bf.FORMAT_VERSION == 1
        assert len(bundle.manifest["bundle_id"]) == 32
        assert bundle.manifest["counts"] == {"conversations": 1, "messages": 1, "user_files": 1}
        # A column outside the allow-list (the owner) is never written.
        assert list(bundle.rows("conversations")) == [
            {"id": 1, "name": "Synthetic chat", "model_id": "model-a"}
        ]
        assert list(bundle.rows("debate_turns")) == []
        (row,) = bundle.rows("user_files")
        assert bundle.blob(row["blob"]) == b"%PDF-1.4 synthetic"
        assert bundle.skill_files() == [
            ("note-taker", "SKILL.md", b"---\nname: note-taker\n---\nbody\n"),
            ("note-taker", "templates/a.txt", b"template"),
        ]
        assert bundle.preferences() == {"interface.theme": "light"}


def test_bytes_values_round_trip_as_hex() -> None:
    buf = io.BytesIO()
    vector = bytes(range(256)) * 6
    with bf.BundleWriter(buf, spark_version="x") as writer:
        writer.add_rows("user_memories", [{"id": 1, "embedding": vector}])
    raw = _members(buf.getvalue())["data/user_memories.jsonl"].decode()
    assert vector.hex() in raw
    with _open(buf.getvalue()) as bundle:
        assert list(bundle.rows("user_memories")) == [{"id": 1, "embedding": vector}]


def test_writer_refuses_an_unknown_table() -> None:
    with pytest.raises(ValueError):
        with bf.BundleWriter(io.BytesIO(), spark_version="x") as writer:
            writer.add_rows("instance_settings", [{"key": "k"}])


def test_writer_refuses_an_unsafe_skill_path() -> None:
    with bf.BundleWriter(io.BytesIO(), spark_version="x") as writer:
        for name, rel in (("ok-name", "../x"), ("Bad Name", "SKILL.md"), ("ok", "/abs")):
            with pytest.raises(ValueError):
                writer.add_skill_file(name, rel, b"")


# -- The version check --------------------------------------------------------------


@pytest.mark.parametrize("version", [0, 2, 99, "1", None, True])
def test_refuses_a_format_version_it_does_not_read(version: Any) -> None:
    members = _members(_bundle())
    doc = json.loads(members[bf.MANIFEST])
    doc["format_version"] = version
    with pytest.raises(BundleRefused, match="version"):
        _open(_rebuild(members, manifest=doc))


def test_refuses_another_format() -> None:
    members = _members(_bundle())
    doc = json.loads(members[bf.MANIFEST])
    doc["format"] = "something-else"
    with pytest.raises(BundleRefused, match="not a Spark user bundle"):
        _open(_rebuild(members, manifest=doc))


def test_refuses_a_missing_or_unreadable_manifest() -> None:
    members = _members(_bundle())
    del members[bf.MANIFEST]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    with pytest.raises(BundleRefused, match="manifest"):
        _open(buf.getvalue())
    members = _members(_bundle())
    members[bf.MANIFEST] = b"{not json"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)
    with pytest.raises(BundleRefused, match="manifest"):
        _open(buf.getvalue())


def test_refuses_something_that_is_not_a_zip() -> None:
    with pytest.raises(BundleRefused, match="not a zip"):
        _open(b"PK\x03\x04 but not really")


def test_refuses_a_bad_bundle_id() -> None:
    members = _members(_bundle())
    doc = json.loads(members[bf.MANIFEST])
    doc["bundle_id"] = "../../x"
    with pytest.raises(BundleRefused, match="bundle id"):
        _open(_rebuild(members, manifest=doc))


# -- Integrity -----------------------------------------------------------------------


def test_refuses_a_member_whose_hash_differs() -> None:
    members = _members(_bundle())
    members["data/messages.jsonl"] = b'{"id": 7, "conversation_id": 1, "content": "changed"}\n'
    with pytest.raises(BundleRefused, match="does not match"):
        _open(_rebuild(members, fix_hashes=False))


def test_refuses_a_member_the_manifest_does_not_list() -> None:
    members = _members(_bundle())
    doc = json.loads(members[bf.MANIFEST])
    members["data/agent_runs.jsonl"] = b""
    with pytest.raises(BundleRefused, match="not listed"):
        _open(_rebuild(members, manifest=doc, fix_hashes=False))


def test_refuses_a_listed_member_that_is_missing() -> None:
    members = _members(_bundle())
    doc = json.loads(members[bf.MANIFEST])
    del members["data/messages.jsonl"]
    with pytest.raises(BundleRefused, match="missing"):
        _open(_rebuild(members, manifest=doc, fix_hashes=False))


def test_refuses_a_blob_named_other_than_its_hash() -> None:
    members = _members(_bundle())
    (name,) = [n for n in members if n.startswith("files/")]
    members["files/" + "0" * 64] = members.pop(name)
    with pytest.raises(BundleRefused, match="does not match its name"):
        _open(_rebuild(members))


def test_refuses_a_row_that_is_not_an_object() -> None:
    members = _members(_bundle())
    members["data/messages.jsonl"] = b"[1, 2]\n"
    with _open(_rebuild(members)) as bundle:
        with pytest.raises(BundleRefused, match="row"):
            list(bundle.rows("messages"))


# -- Archive containment ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "/etc/passwd",
        "C:/Windows/x",
        "data/../../x.jsonl",
        "skills/../../x/SKILL.md",
        "skills/note-taker/../../../x",
        "skills/note-taker/.hidden",
        "skills/note-taker/a/../b",
        "skills/Bad Name/SKILL.md",
        "skills/note-taker/CON",
        "skills/note-taker/a:b",
        "files/abc",
        "data/instance_settings.jsonl",
        "data/conversations.json",
        "config.yaml",
        "secrets.json",
        "data/",
        "\x00x",
    ],
)
def test_refuses_a_member_name_outside_the_layout(name: str) -> None:
    members = _members(_bundle())
    members[name] = b"x"
    with pytest.raises(BundleRefused, match="member name"):
        _open(_rebuild(members))


def test_refuses_a_symbolic_link_member() -> None:
    members = _members(_bundle())
    info = zipfile.ZipInfo("skills/note-taker/link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(BundleRefused, match="not a regular file"):
        _open(_rebuild(members, extra_infos=[(info, b"/etc/passwd")]))


def test_refuses_a_duplicate_member() -> None:
    data = _bundle()
    members = _members(data)
    buf = io.BytesIO(data)
    with zipfile.ZipFile(buf, "a") as zf, pytest.warns(UserWarning):
        zf.writestr("data/messages.jsonl", members["data/messages.jsonl"])
    with pytest.raises(BundleRefused, match="more than once"):
        _open(buf.getvalue())


def _patch_central(data: bytes, name: str, offset: int, value: bytes) -> bytes:
    """``data`` with ``value`` written at ``offset`` of ``name``'s central directory entry."""
    out = bytearray(data)
    at = out.find(name.encode(), out.find(b"PK\x01\x02"))
    entry = out.rfind(b"PK\x01\x02", 0, at)
    out[entry + offset : entry + offset + len(value)] = value
    return bytes(out)


def test_refuses_an_encrypted_member() -> None:
    data = _rebuild(_members(_bundle()))
    # General purpose flag bit 0 (encrypted) of the messages member.
    data = _patch_central(data, "data/messages.jsonl", 8, (1).to_bytes(2, "little"))
    with pytest.raises(BundleRefused, match="encrypted"):
        _open(data)


def test_a_backslash_name_is_never_taken_as_a_path() -> None:
    members = _members(_bundle())
    members["dataXconversations.jsonl"] = b"x"
    data = _rebuild(members).replace(b"dataXconversations", b"data\\conversations")
    # POSIX reads the name as it is (outside the layout); Windows reads the
    # backslash as a separator, which repeats a member.
    with pytest.raises(BundleRefused, match="member name|more than once"):
        _open(data)


# -- The size limit -------------------------------------------------------------------


def test_refuses_an_archive_over_the_bundle_limit() -> None:
    data = _bundle()
    with pytest.raises(BundleRefused, match="larger than"):
        _open(data, Limits(max_bundle_bytes=len(data) - 1))
    with _open(data, Limits(max_bundle_bytes=len(data))):
        pass


def test_refuses_contents_over_the_unpacked_limit() -> None:
    data = _bundle()
    total = sum(len(body) for body in _members(data).values())
    with pytest.raises(BundleRefused, match="unpacked"):
        _open(data, Limits(max_unpacked_bytes=total - 1))
    with _open(data, Limits(max_unpacked_bytes=total)):
        pass


def test_refuses_too_many_members() -> None:
    data = _bundle()
    count = len(_members(data))
    with pytest.raises(BundleRefused, match="members"):
        _open(data, Limits(max_members=count - 1))
    with _open(data, Limits(max_members=count)):
        pass


def test_refuses_a_member_that_expands_too_far() -> None:
    members = _members(_bundle())
    members["skills/note-taker/zeros.txt"] = b"\0" * (4 << 20)
    with pytest.raises(BundleRefused, match="compression"):
        _open(_rebuild(members), Limits(max_ratio=100))


def test_a_declared_size_that_lies_is_caught_while_reading() -> None:
    members = _members(_bundle())
    # Shrink the declared uncompressed size of messages in its central directory entry.
    data = _patch_central(_rebuild(members), "data/messages.jsonl", 24, (1).to_bytes(4, "little"))
    with pytest.raises(BundleRefused):
        with _open(data) as bundle:
            list(bundle.rows("messages"))
