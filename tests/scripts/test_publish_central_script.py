"""publish-central.sh (.github/actions/publish-central) against stub ``crane`` and ``cosign``.

The fake registry copies by digest the way crane does, and can be told to land a copy at
another digest (as ``docker buildx imagetools create`` does when it wraps one manifest in a
new image index, which is what stopped Kvasir's first central push). Every run checks that
the AWS identifiers are masked first and never printed, and that nothing is signed unless the
central tag names the published digest.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests.scripts import image_helpers as h

SOURCE = f"{h.GHCR}:{h.VERSION}"
CENTRAL = f"{h.CENTRAL_URI}:{h.VERSION}"
SIGNATURE_TAG = f"{h.CENTRAL_URI}:sha256-{'a' * 64}.sig"


def model(**overrides: Any) -> dict[str, Any]:
    registry = h.registry(**overrides)
    registry["tags"][SOURCE] = h.DIGEST_A
    return registry


def publish(tmp_path: Path, registry: dict[str, Any], version: str = h.VERSION) -> h.Run:
    run = h.run(
        tmp_path,
        [h.PUBLISH_CENTRAL_SCRIPT.as_posix(), version],
        model=registry,
        env={
            "CENTRAL_ECR_REPOSITORY_URI": h.CENTRAL_URI,
            "IMAGE_SIGNING_KEY": h.KMS_KEY,
            "GITHUB_REPOSITORY": h.GITHUB_REPOSITORY,
        },
    )
    _check_rules(run)
    return run


def _check_rules(run: h.Run) -> None:
    h.assert_nothing_identifying(run)
    if run.returncode == 2:
        return
    lines = run.output.splitlines()
    first = next(n for n, line in enumerate(lines) if not line.startswith("::add-mask::"))
    assert all(line.startswith("::add-mask::") for line in lines[:first])
    for identifier in (h.CENTRAL_URI, h.CENTRAL_HOST, h.CENTRAL_ACCOUNT, h.KMS_KEY, h.KEY_ARN):
        assert identifier in "\n".join(lines[:first]), f"{identifier} was not masked first"
    registry = run.state["registry"]
    for call in run.tool_calls("cosign", "sign"):
        assert call[-1] == f"{h.CENTRAL_URI}@{h.DIGEST_A}", "only the published digest is signed"
        assert "--tlog-upload=false" in call
        assert "--yes" in call
        assert call[call.index("--key") + 1] == h.KMS_KEY
        assert registry["tags"].get(CENTRAL) == h.DIGEST_A
    for call in run.tool_calls("cosign", "verify"):
        assert "--insecure-ignore-tlog=true" in call
        assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", call[-1])
    assert not any("imagetools" in " ".join(call) for call in run.calls)


def names(run: h.Run) -> list[str]:
    return [f"{c[0]} {c[1]}" for c in run.calls]


def test_a_first_push_copies_byte_for_byte_signs_and_verifies(tmp_path: Path) -> None:
    run = publish(tmp_path, model())
    assert run.returncode == 0, run.output
    assert run.tool_calls("crane", "copy") == [["crane", "copy", SOURCE, CENTRAL]]
    registry = run.state["registry"]
    assert registry["tags"][CENTRAL] == h.DIGEST_A
    assert registry["signatures"] == {f"{h.CENTRAL_URI}@{h.DIGEST_A}": "release"}
    order = names(run)
    assert order.index("crane copy") < order.index("cosign sign")
    assert order.index("cosign sign") < len(order) - 1 - order[::-1].index("cosign verify")
    assert f"digest={h.DIGEST_A}" in run.outputs
    assert h.DIGEST_A in run.summary
    assert h.RELEASE_PEM in run.summary
    assert "signed, verified" in run.summary


def test_a_rerun_after_success_copies_and_signs_nothing(tmp_path: Path) -> None:
    registry = model()
    registry["tags"][CENTRAL] = h.DIGEST_A
    h.signed(registry, h.CENTRAL_URI, h.DIGEST_A)
    run = publish(tmp_path, registry)
    assert run.returncode == 0, run.output
    assert run.tool_calls("crane", "copy") == []
    assert run.tool_calls("cosign", "sign") == []
    assert "already verifies" in run.output
    assert "already signed" in run.summary
    assert f"digest={h.DIGEST_A}" in run.outputs


def test_a_rerun_after_the_copy_signs_without_copying(tmp_path: Path) -> None:
    registry = model()
    registry["tags"][CENTRAL] = h.DIGEST_A
    run = publish(tmp_path, registry)
    assert run.returncode == 0, run.output
    assert run.tool_calls("crane", "copy") == []
    assert len(run.tool_calls("cosign", "sign")) == 1


def test_a_central_tag_holding_another_digest_is_refused_and_nothing_is_signed(
    tmp_path: Path,
) -> None:
    registry = model()
    registry["tags"][CENTRAL] = h.DIGEST_B
    run = publish(tmp_path, registry)
    assert run.returncode == 1
    assert "different digest" in run.output
    assert run.tool_calls("crane", "copy") == []
    assert run.tool_calls("cosign", "sign") == []
    assert run.outputs == ""


def test_a_copy_that_lands_as_another_digest_is_refused_before_signing(tmp_path: Path) -> None:
    # The lesson of Kvasir's first central push: a wrapped copy has a new digest.
    run = publish(tmp_path, model(copy_rewrites_to=h.DIGEST_C))
    assert run.returncode == 1
    assert len(run.tool_calls("crane", "copy")) == 1
    assert run.tool_calls("cosign", "sign") == []
    assert run.state["registry"]["signatures"] == {}
    assert "differs from the published one" in run.output
    assert run.outputs == ""


def test_a_signature_tag_that_does_not_verify_stops_the_job(tmp_path: Path) -> None:
    registry = model()
    registry["tags"][CENTRAL] = h.DIGEST_A
    h.signed(registry, h.CENTRAL_URI, h.DIGEST_A, "other")
    run = publish(tmp_path, registry)
    assert run.returncode == 1
    assert "does not verify with the release key" in run.output
    assert run.tool_calls("cosign", "sign") == []


def test_a_refused_kms_sign_fails_without_printing_an_arn(tmp_path: Path) -> None:
    run = publish(tmp_path, model(sign_denied=True))
    assert run.returncode == 1
    assert "AccessDeniedException" in run.output
    assert "arn:***" in run.output
    assert "kms:DescribeKey" in run.output
    assert run.outputs == ""


def test_a_missing_published_image_fails_before_any_copy(tmp_path: Path) -> None:
    registry = h.registry()
    run = publish(tmp_path, registry)
    assert run.returncode == 1
    assert run.tool_calls("crane", "copy") == []


@pytest.mark.parametrize("version", ["0.3.0a5", "0.3.0b1", "0.3.0.dev1", "bad;version", ""])
def test_only_final_versions_and_release_candidates_are_published(
    tmp_path: Path, version: str
) -> None:
    run = publish(tmp_path, model(), version)
    assert run.returncode == 2
    assert run.calls == []


def test_a_key_that_is_not_a_kms_reference_is_refused(tmp_path: Path) -> None:
    run = h.run(
        tmp_path,
        [h.PUBLISH_CENTRAL_SCRIPT.as_posix(), h.VERSION],
        model=model(),
        env={
            "CENTRAL_ECR_REPOSITORY_URI": h.CENTRAL_URI,
            "IMAGE_SIGNING_KEY": "cosign.key",
            "GITHUB_REPOSITORY": h.GITHUB_REPOSITORY,
        },
    )
    assert run.returncode == 2
    assert run.calls == []


def test_the_central_copy_uses_the_shared_copy_script() -> None:
    text = h.PUBLISH_CENTRAL_SCRIPT.read_text(encoding="utf-8")
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert any("copy-image.sh" in line for line in code)
    assert not any("imagetools" in line for line in code)
    assert not re.search(r"\b\d{12}\b", text)
    assert "amazonaws.com" not in text
