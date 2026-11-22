"""Shared set-up for the image script tests: invented registries, keys and the stub shims.

Every name here is invented: the account IDs are AWS's documentation examples, and the
keys are not keys.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.scripts import registry_stub

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
VERIFY_SCRIPT = ROOT / "scripts" / "verify-image.sh"
PUBLISH_CENTRAL_SCRIPT = ROOT / ".github" / "actions" / "publish-central" / "publish-central.sh"

VERSION = "0.3.0rc38"
GITHUB_REPOSITORY = "Example-Org/Example-Spark"
GHCR = "ghcr.io/example-org/example-spark"
TEST_ACCOUNT = "123456789012"
TEST_HOST = f"{TEST_ACCOUNT}.dkr.ecr.ap-southeast-2.amazonaws.com"
TEST_URI = f"{TEST_HOST}/example-spark-test"
CENTRAL_ACCOUNT = "111122223333"
CENTRAL_HOST = f"{CENTRAL_ACCOUNT}.dkr.ecr.ap-southeast-2.amazonaws.com"
CENTRAL_URI = f"{CENTRAL_HOST}/example-spark"
KEY_ID = "0000aaaa-bb11-cc22-dd33-eeeeee444444"
KEY_ARN = f"arn:aws:kms:ap-southeast-2:{CENTRAL_ACCOUNT}:key/{KEY_ID}"
KMS_KEY = f"awskms:///{KEY_ARN}"
RELEASE_PEM = "-----BEGIN PUBLIC KEY-----\nRELEASEKEYEXAMPLEONLY\n-----END PUBLIC KEY-----"
OTHER_PEM = "-----BEGIN PUBLIC KEY-----\nSOMEOTHERKEYEXAMPLE\n-----END PUBLIC KEY-----"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64

# Identifiers that must never be printed except in an ::add-mask:: line.
IDENTIFIERS = [TEST_ACCOUNT, TEST_HOST, CENTRAL_ACCOUNT, CENTRAL_HOST, KEY_ID, KEY_ARN]


def registry(**overrides: Any) -> dict[str, Any]:
    """A registry with the release key and another key, and nothing published."""
    model = registry_stub.empty_registry(
        keys={KMS_KEY: "release", RELEASE_PEM: "release", OTHER_PEM: "other"},
        public_keys={"release": RELEASE_PEM, "other": OTHER_PEM},
    )
    model.update(overrides)
    return model


def signed(model: dict[str, Any], repository: str, digest: str, key: str = "release") -> None:
    """Record a cosign v2 signature of ``repository@digest`` by ``key`` in ``model``."""
    model["signatures"][f"{repository}@{digest}"] = key
    model["tags"][f"{repository}:sha256-{digest.removeprefix('sha256:')}.sig"] = (
        "sha256:" + "5" * 64
    )


def shim(directory: Path, tool: str, stub: str) -> None:
    path = directory / tool
    python = Path(shutil.which("python") or "python").resolve().as_posix()
    path.write_text(
        f'#!/usr/bin/env bash\nexec "{python}" "{(HERE / stub).as_posix()}" {tool} "$@"\n',
        newline="\n",
    )
    path.chmod(0o755)


@dataclass
class Run:
    returncode: int
    output: str
    stdout: str
    summary: str
    outputs: str
    calls: list[list[str]]
    state: dict[str, Any]

    def tool_calls(self, tool: str, command: str | None = None) -> list[list[str]]:
        return [c for c in self.calls if c[0] == tool and (command is None or c[1:2] == [command])]

    @property
    def visible(self) -> str:
        return "\n".join(
            line for line in self.output.splitlines() if not line.startswith("::add-mask::")
        )

    @property
    def masked(self) -> str:
        return "\n".join(
            line for line in self.output.splitlines() if line.startswith("::add-mask::")
        )


def run(
    tmp_path: Path,
    command: list[str],
    *,
    model: dict[str, Any],
    env: dict[str, str] | None = None,
    state: dict[str, Any] | None = None,
    aws: bool = False,
) -> Run:
    """Run ``bash command...`` with the registry stub (and the aws stub when ``aws``)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim(bin_dir, "crane", "registry_stub.py")
    shim(bin_dir, "cosign", "registry_stub.py")
    if aws:
        shim(bin_dir, "aws", "aws_stub.py")
        shim(bin_dir, "curl", "aws_stub.py")
    state_path = tmp_path / "state.json"
    full_state = {"calls": [], **(state or {}), "registry": model}
    state_path.write_text(json.dumps(full_state), encoding="utf-8")
    summary_path = tmp_path / "summary.md"
    output_path = tmp_path / "output.txt"
    environment = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
    environment.update(
        {
            "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
            "STUB_STATE": state_path.as_posix(),
            "GITHUB_STEP_SUMMARY": summary_path.as_posix(),
            "GITHUB_OUTPUT": output_path.as_posix(),
            **(env or {}),
        }
    )
    bash = shutil.which("bash")
    assert bash, "bash is required"
    result = subprocess.run(
        [bash, *command],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    final = json.loads(state_path.read_text(encoding="utf-8"))
    return Run(
        returncode=result.returncode,
        output=result.stdout + result.stderr,
        stdout=result.stdout,
        summary=summary_path.read_text(encoding="utf-8") if summary_path.exists() else "",
        outputs=output_path.read_text(encoding="utf-8") if output_path.exists() else "",
        calls=final["calls"],
        state=final,
    )


def assert_nothing_identifying(run: Run, identifiers: list[str] | None = None) -> None:
    for identifier in identifiers or IDENTIFIERS:
        assert identifier not in run.visible, f"{identifier} is printed unmasked"
        assert identifier not in run.summary, f"{identifier} is in the job summary"
