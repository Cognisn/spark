"""scripts/verify-image.sh against stub ``crane`` and ``cosign`` (tests/scripts/registry_stub.py).

The script must exit 0 only when the tag resolves to the expected digest and that digest
carries a signature by the given key; 1 when anything does not match; 2 on a usage error,
before calling anything.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests.scripts import image_helpers as h

REFERENCE = f"{h.CENTRAL_URI}:{h.VERSION}"


def published(digest: str = h.DIGEST_A, *, sign: str | None = "release") -> dict[str, Any]:
    model = h.registry()
    model["tags"][REFERENCE] = digest
    if sign:
        h.signed(model, h.CENTRAL_URI, digest, sign)
    return model


def key_file(tmp_path: Path, pem: str = h.RELEASE_PEM) -> str:
    path = tmp_path / "release.pub"
    path.write_text(pem + "\n", newline="\n")
    return path.as_posix()


def verify(tmp_path: Path, *args: str, model: dict[str, Any], **env: str) -> h.Run:
    return h.run(tmp_path, [h.VERIFY_SCRIPT.as_posix(), *args], model=model, env=env)


# ---- verified -----------------------------------------------------------------------


def test_a_signed_tag_verifies_with_the_public_key_file(tmp_path: Path) -> None:
    run = verify(tmp_path, "--key", key_file(tmp_path), REFERENCE, model=published())
    assert run.returncode == 0, run.output
    assert f"verified: tag {h.VERSION} is {h.DIGEST_A}" in run.stdout
    assert run.tool_calls("crane", "digest") == [["crane", "digest", REFERENCE]]
    (call,) = run.tool_calls("cosign", "verify")
    assert call[-1] == f"{h.CENTRAL_URI}@{h.DIGEST_A}"
    assert "--insecure-ignore-tlog=true" in call


def test_a_signed_tag_verifies_with_the_kms_key(tmp_path: Path) -> None:
    run = verify(tmp_path, "--kms-key", h.KMS_KEY, REFERENCE, model=published())
    assert run.returncode == 0, run.output
    (call,) = run.tool_calls("cosign", "verify")
    assert call[call.index("--key") + 1] == h.KMS_KEY


@pytest.mark.parametrize("where", ["--digest", "in the reference"])
def test_the_expected_digest_is_accepted_when_it_matches(tmp_path: Path, where: str) -> None:
    args = (
        ["--digest", h.DIGEST_A, REFERENCE]
        if where == "--digest"
        else [f"{REFERENCE}@{h.DIGEST_A}"]
    )
    run = verify(tmp_path, "--key", key_file(tmp_path), *args, model=published())
    assert run.returncode == 0, run.output


# ---- not verified -------------------------------------------------------------------


@pytest.mark.parametrize("where", ["--digest", "in the reference"])
def test_a_tag_holding_another_digest_fails(tmp_path: Path, where: str) -> None:
    model = published(h.DIGEST_B)
    h.signed(model, h.CENTRAL_URI, h.DIGEST_A)  # the expected digest is signed too
    args = (
        ["--digest", h.DIGEST_A, REFERENCE]
        if where == "--digest"
        else [f"{REFERENCE}@{h.DIGEST_A}"]
    )
    run = verify(tmp_path, "--key", key_file(tmp_path), *args, model=model)
    assert run.returncode == 1
    assert "NOT VERIFIED" in run.output
    assert h.DIGEST_B in run.output
    assert run.tool_calls("cosign") == []


def test_an_unsigned_image_fails(tmp_path: Path) -> None:
    run = verify(tmp_path, "--key", key_file(tmp_path), REFERENCE, model=published(sign=None))
    assert run.returncode == 1
    assert "no valid signature by the release key" in run.output


def test_an_image_signed_by_another_key_fails(tmp_path: Path) -> None:
    run = verify(tmp_path, "--key", key_file(tmp_path), REFERENCE, model=published(sign="other"))
    assert run.returncode == 1
    assert "no valid signature" in run.output


def test_the_wrong_public_key_fails(tmp_path: Path) -> None:
    run = verify(tmp_path, "--key", key_file(tmp_path, h.OTHER_PEM), REFERENCE, model=published())
    assert run.returncode == 1


def test_a_signature_of_another_digest_does_not_count(tmp_path: Path) -> None:
    model = published(h.DIGEST_B, sign=None)
    h.signed(model, h.CENTRAL_URI, h.DIGEST_A)
    run = verify(tmp_path, "--key", key_file(tmp_path), REFERENCE, model=model)
    assert run.returncode == 1
    (call,) = run.tool_calls("cosign", "verify")
    assert call[-1] == f"{h.CENTRAL_URI}@{h.DIGEST_B}"


def test_a_signature_whose_payload_names_another_digest_fails(tmp_path: Path) -> None:
    model = published()
    model["claim_digest"] = h.DIGEST_C
    run = verify(tmp_path, "--key", key_file(tmp_path), REFERENCE, model=model)
    assert run.returncode == 1
    assert "does not name" in run.output


def test_a_missing_tag_fails(tmp_path: Path) -> None:
    run = verify(tmp_path, "--key", key_file(tmp_path), REFERENCE, model=h.registry())
    assert run.returncode == 1
    assert run.tool_calls("cosign") == []


# ---- usage --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        [],
        [REFERENCE],
        ["--key", "KEYFILE", "--kms-key", h.KMS_KEY, REFERENCE],
        ["--kms-key", "arn:aws:kms:ap-southeast-2:111122223333:key/x", REFERENCE],
        ["--key", "KEYFILE", h.CENTRAL_URI],
        ["--key", "KEYFILE", f"{REFERENCE}@sha256:abc"],
        ["--key", "KEYFILE", "--digest", "sha256:abc", REFERENCE],
        ["--key", "KEYFILE", REFERENCE, "extra"],
        ["--key", "KEYFILE", "--unknown", REFERENCE],
        ["--key", "MISSING", REFERENCE],
        ["--key"],
    ],
)
def test_usage_errors_exit_2_before_any_call(tmp_path: Path, args: list[str]) -> None:
    real = key_file(tmp_path)
    args = [
        real if a == "KEYFILE" else (tmp_path / "nope").as_posix() if a == "MISSING" else a
        for a in args
    ]
    run = verify(tmp_path, *args, model=published())
    assert run.returncode == 2, run.output
    assert run.calls == []


# ---- in GitHub Actions --------------------------------------------------------------


@pytest.mark.parametrize("signer", ["release", None])
def test_in_actions_identifiers_are_masked_first_and_never_printed(
    tmp_path: Path, signer: str | None
) -> None:
    run = verify(
        tmp_path,
        "--kms-key",
        h.KMS_KEY,
        REFERENCE,
        model=published(sign=signer),
        GITHUB_ACTIONS="true",
    )
    assert run.returncode == (0 if signer else 1)
    lines = run.output.splitlines()
    masks = [n for n, line in enumerate(lines) if line.startswith("::add-mask::")]
    others = [n for n, line in enumerate(lines) if not line.startswith("::add-mask::")]
    assert masks and (not others or masks[-1] < others[0])
    for identifier in (h.CENTRAL_URI, h.CENTRAL_HOST, h.KMS_KEY, h.KEY_ARN, h.KEY_ID):
        assert identifier in run.masked, f"{identifier} was never masked"
    h.assert_nothing_identifying(run)


def test_the_script_names_no_identifier() -> None:
    text = h.VERIFY_SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{12}\b", text)
    assert "amazonaws.com" not in text
