"""scripts/copy-image.sh against a stub ``crane`` that records its calls.

The stub keeps its model in a directory: ``source_digest`` is what ghcr.io holds, ``ecr_digest``
(absent means the tag is missing) is what ECR holds, ``copy_result`` is the digest ECR reports
after a copy, and ``fail_source``, ``fail_copy`` and ``fail_ecr_read`` switch on failures.
Every run asserts the standing rules: nothing but crane is called, the masks are the first
lines, and no AWS identifier appears in the output except in an ``::add-mask::`` line.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent.parent / "scripts" / "copy-image.sh"
VERSION = "0.3.0rc22"
ACCOUNT = "123456789012"
ECR_HOST = f"{ACCOUNT}.dkr.ecr.ap-southeast-2.amazonaws.com"
ECR_URI = f"{ECR_HOST}/example-spark"
REPOSITORY = "Example-Org/Example-Spark"
SOURCE = f"ghcr.io/example-org/example-spark:{VERSION}"
TARGET = f"{ECR_URI}:{VERSION}"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64

STUB = r"""#!/usr/bin/env bash
state="$STUB_STATE"
echo "$*" >> "$state/calls"
case "$1" in
    digest)
        if [ "$2" = "$STUB_SOURCE" ]; then
            if [ -e "$state/fail_source" ]; then
                echo "UNAUTHORIZED: authentication required for $2" >&2
                exit 1
            fi
            cat "$state/source_digest"
        else
            if [ -e "$state/fail_ecr_read" ]; then
                echo "dial tcp: lookup $STUB_ECR_HOST: no such host" >&2
                exit 1
            fi
            if [ -e "$state/copied" ]; then
                cat "$state/copy_result"
            elif [ -e "$state/ecr_digest" ]; then
                cat "$state/ecr_digest"
            else
                echo "GET https://$STUB_ECR_HOST/v2/example-spark/manifests/x: MANIFEST_UNKNOWN: Requested image not found" >&2
                exit 1
            fi
        fi
        ;;
    copy)
        if [ -e "$state/fail_copy" ]; then
            echo "UNAUTHORIZED: not authorized to push to $STUB_ECR_URI" >&2
            exit 1
        fi
        touch "$state/copied"
        ;;
    *)
        echo "unexpected crane command: $*" >&2
        exit 2
        ;;
esac
"""


@dataclass
class Run:
    returncode: int
    output: str
    calls: list[str]


def copy_image(
    tmp_path: Path,
    *,
    source_digest: str = DIGEST_A,
    ecr_digest: str | None = None,
    copy_result: str = DIGEST_A,
    fail_source: bool = False,
    fail_copy: bool = False,
    fail_ecr_read: bool = False,
) -> Run:
    bin_dir = tmp_path / "bin"
    state = tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    shim = bin_dir / "crane"
    shim.write_text(STUB, newline="\n")
    shim.chmod(0o755)
    (state / "calls").write_text("", newline="\n")
    (state / "source_digest").write_text(source_digest, newline="\n")
    (state / "copy_result").write_text(copy_result, newline="\n")
    if ecr_digest is not None:
        (state / "ecr_digest").write_text(ecr_digest, newline="\n")
    for name, on in (
        ("fail_source", fail_source),
        ("fail_copy", fail_copy),
        ("fail_ecr_read", fail_ecr_read),
    ):
        if on:
            (state / name).write_text("", newline="\n")
    environment = {
        **os.environ,
        "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
        "STUB_STATE": state.as_posix(),
        "STUB_SOURCE": SOURCE,
        "STUB_ECR_HOST": ECR_HOST,
        "STUB_ECR_URI": ECR_URI,
        "ECR_REPOSITORY_URI": ECR_URI,
        "GITHUB_REPOSITORY": REPOSITORY,
    }
    bash = shutil.which("bash")
    assert bash, "bash is required"
    result = subprocess.run(
        [bash, SCRIPT.as_posix(), VERSION],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    calls = (state / "calls").read_text(encoding="utf-8").splitlines()
    run = Run(result.returncode, result.stdout + result.stderr, calls)
    _check_rules(run)
    return run


def _check_rules(run: Run) -> None:
    assert all(call.split()[0] in {"digest", "copy"} for call in run.calls)
    lines = run.output.splitlines()
    for identifier in (ECR_HOST, ECR_URI, ACCOUNT):
        for line in lines:
            if identifier in line:
                assert line.startswith("::add-mask::"), f"{identifier} printed: {line}"
    other = [n for n, line in enumerate(lines) if not line.startswith("::add-mask::")]
    masks = [n for n, line in enumerate(lines) if line.startswith("::add-mask::")]
    assert masks, "nothing was masked"
    assert not other or masks[-1] < other[0], "a line was printed before the masks"


def copies(run: Run) -> list[str]:
    return [call for call in run.calls if call.startswith("copy")]


def test_copies_when_the_tag_is_missing_and_digests_match(tmp_path: Path) -> None:
    run = copy_image(tmp_path)
    assert run.returncode == 0, run.output
    assert copies(run) == [f"copy {SOURCE} {TARGET}"]
    assert DIGEST_A in run.output
    assert run.calls[-1] == f"digest {TARGET}"


def test_the_source_is_lower_cased(tmp_path: Path) -> None:
    run = copy_image(tmp_path)
    assert f"digest {SOURCE}" in run.calls
    assert not any("Example-Org" in call for call in run.calls)


def test_the_tag_already_present_with_the_same_digest_succeeds_without_a_copy(
    tmp_path: Path,
) -> None:
    run = copy_image(tmp_path, ecr_digest=DIGEST_A)
    assert run.returncode == 0, run.output
    assert copies(run) == []
    assert "already in ECR with the same digest" in run.output


def test_the_tag_already_present_with_another_digest_fails_without_a_copy(
    tmp_path: Path,
) -> None:
    run = copy_image(tmp_path, ecr_digest=DIGEST_B)
    assert run.returncode == 1
    assert copies(run) == []
    assert VERSION in run.output
    assert "immutable tag cannot be overwritten" in run.output
    assert "new release candidate" in run.output


def test_a_copy_that_comes_back_with_another_digest_fails(tmp_path: Path) -> None:
    run = copy_image(tmp_path, copy_result=DIGEST_B)
    assert run.returncode == 1
    assert len(copies(run)) == 1
    assert "differs from the published one" in run.output


def test_a_failed_source_read_fails_without_a_copy(tmp_path: Path) -> None:
    run = copy_image(tmp_path, fail_source=True)
    assert run.returncode == 1
    assert copies(run) == []
    assert "ghcr.io" in run.output


def test_a_failed_copy_fails_and_hides_the_registry_address(tmp_path: Path) -> None:
    run = copy_image(tmp_path, fail_copy=True)
    assert run.returncode == 1
    assert len(copies(run)) == 1
    assert "the copy of" in run.output
    assert "UNAUTHORIZED" in run.output
    assert "not authorized to push to ***" in run.output


def test_an_ecr_read_that_is_not_a_missing_tag_fails_without_a_copy(tmp_path: Path) -> None:
    run = copy_image(tmp_path, fail_ecr_read=True)
    assert run.returncode == 1
    assert copies(run) == []
    assert "not a missing tag" in run.output


@pytest.mark.parametrize("digest", ["", "sha256:abc", "not-a-digest"])
def test_a_source_digest_that_is_not_a_digest_fails(tmp_path: Path, digest: str) -> None:
    run = copy_image(tmp_path, source_digest=digest)
    assert run.returncode == 1
    assert copies(run) == []


def test_a_bad_version_is_refused() -> None:
    bash = shutil.which("bash")
    assert bash
    result = subprocess.run(
        [bash, SCRIPT.as_posix(), "--oops"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 2


def test_the_script_names_no_identifier() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{12}\b", text)
    assert "amazonaws.com" not in text
