"""Credentials never travel in a bundle: redacted on export, refused on import.

Every credential here is synthetic and assembled at run time, so no string
shaped like a real key appears in the repository.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from spark.bundles import format as bf
from spark.bundles import secrets
from spark.bundles.reader import BundleRefused, Limits, open_bundle

SHAPES = {
    "anthropic or openai key": "sk-" + "ant-" + "x1" * 14,
    "aws access key": "AK" + "IA" + "EXAMPLE0" * 2,
    "google api key": "AI" + "za" + "S" * 35,
    "xai key": "xa" + "i-" + "Q" * 24,
    "github token": "gh" + "p_" + "z" * 36,
    "slack token": "xo" + "xb-" + "1234567890",
    "elevenlabs key": "sk" + "_" + "ab" * 24,
    "warden api key": "wd" + "n_" + "k" * 20,
    "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
}


@pytest.mark.parametrize("kind", sorted(SHAPES))
def test_each_credential_shape_is_found_and_redacted(kind: str) -> None:
    text = f"before {SHAPES[kind]} after"
    assert kind in secrets.find(text.encode())
    redacted, count = secrets.redact(text)
    assert count >= 1
    assert SHAPES[kind] not in redacted
    assert secrets.find(redacted.encode()) == []
    assert redacted.startswith("before ") and redacted.endswith(" after")


def test_ordinary_text_is_left_alone() -> None:
    text = "Ask about the sk- prefix, AKIA in general, and a desk-lamp."
    assert secrets.find(text.encode()) == []
    assert secrets.redact(text) == (text, 0)


def test_the_instance_secret_values_are_redacted_wherever_they_appear() -> None:
    value = "brave-" + "search-" + "value-1234"
    text = f"my key is {value}, again {value}"
    redacted, count = secrets.redact(text, [value, "short"])
    assert count == 2
    assert value not in redacted
    # Values too short to be credentials are not replaced (they are ordinary words).
    assert secrets.redact("a short word", ["short"]) == ("a short word", 0)


def test_a_chunked_scan_finds_a_credential_across_a_chunk_boundary() -> None:
    shape = SHAPES["github token"].encode()
    data = b"x" * 1000 + b" " + shape + b" " + b"y" * 1000
    for chunk in (7, 64, 1010, 4096):
        assert "github token" in set(secrets.find_in_stream(io.BytesIO(data), chunk_size=chunk))


def _with_member(name: str, body: bytes) -> bytes:
    buf = io.BytesIO()
    with bf.BundleWriter(buf, spark_version="x") as writer:
        if name.startswith("data/"):
            table = name[5:-6]
            writer.add_rows(table, [json.loads(body)])
        elif name.startswith("skills/"):
            _, skill, rel = name.split("/", 2)
            writer.add_skill_file(skill, rel, body)
        elif name == bf.PREFERENCES:
            writer.set_preferences(json.loads(body))
        else:
            writer.add_blob(body)
    return buf.getvalue()


@pytest.mark.parametrize(
    "name,template",
    [
        ("data/messages.jsonl", '{{"id": 1, "conversation_id": 1, "content": "{}"}}'),
        ("data/autonomous_actions.jsonl", '{{"id": 1, "name": "a", "action_prompt": "{}"}}'),
        ("skills/notes/SKILL.md", "---\nname: notes\n---\n{}\n"),
        (bf.PREFERENCES, '{{"conversation.global_instructions": "{}"}}'),
        ("files/", "binary {} content"),
    ],
)
def test_import_refuses_a_bundle_holding_a_credential_anywhere(name: str, template: str) -> None:
    data = _with_member(name, template.format(SHAPES["aws access key"]).encode())
    with pytest.raises(BundleRefused, match="credential"):
        open_bundle(io.BytesIO(data), Limits())


def test_import_refuses_a_credential_in_the_manifest() -> None:
    buf = io.BytesIO()
    with bf.BundleWriter(buf, spark_version="x") as writer:
        writer.set_meta("note", SHAPES["anthropic or openai key"])
    with pytest.raises(BundleRefused, match="credential"):
        open_bundle(io.BytesIO(buf.getvalue()), Limits())


def test_a_clean_bundle_opens() -> None:
    data = _with_member("data/messages.jsonl", b'{"id": 1, "conversation_id": 1, "content": "hi"}')
    with open_bundle(io.BytesIO(data), Limits()) as bundle:
        assert [r["content"] for r in bundle.rows("messages")] == ["hi"]
    assert zipfile.is_zipfile(io.BytesIO(data))
