"""The shape of the image release workflows: pinning, the gate's place, and the job graph.

These read .github/workflows/container.yml, container-publish.yml and the composite actions
under .github/actions/ as data; nothing is run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
ACTIONS = ROOT / ".github" / "actions"
FILES = [
    WORKFLOWS / "container.yml",
    WORKFLOWS / "container-publish.yml",
    *sorted(ACTIONS.glob("*/action.yml")),
]
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(/[\w./-]+)?@[0-9a-f]{40}$")


def load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def steps_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    if "runs" in data:
        return list(data["runs"]["steps"])
    found: list[dict[str, Any]] = []
    for job in data["jobs"].values():
        found.extend(job.get("steps", []))
    return found


def uses_lines(path: Path) -> list[str]:
    return [
        line.split("uses:", 1)[1].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*(-\s+)?uses:", line)
    ]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_third_party_action_is_pinned_by_commit(path: Path) -> None:
    for value in uses_lines(path):
        reference, _, comment = value.partition("#")
        reference = reference.strip()
        if reference.startswith("./") or reference.startswith("actions/"):
            continue  # local actions, and GitHub's own
        assert PINNED.match(reference), f"{reference} in {path.name} is not pinned by commit"
        assert re.match(r"\s*v\d+\.\d+\.\d+", comment), f"{reference} has no version comment"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_no_aws_identifier_is_written_down(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{12}\b", text)
    assert "amazonaws.com" not in text
    assert not re.search(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:\d", text)


def test_the_gate_runs_on_the_loaded_image_before_the_push() -> None:
    steps = steps_of(load(WORKFLOWS / "container.yml"))
    names = [step.get("name", step.get("uses", "")) for step in steps]
    gate = next(
        i for i, s in enumerate(steps) if s.get("uses") == "./.github/actions/vulnerability-gate"
    )
    check = next(i for i, n in enumerate(names) if n.startswith("Check the image"))
    login = next(i for i, n in enumerate(names) if n == "Log in to ghcr.io")
    push = next(i for i, n in enumerate(names) if n == "Push the checked image")
    assert check < gate < login < push
    assert "if" not in steps[gate], "the gate runs on every build, pushed or not"
    assert steps[gate]["with"]["image"] == "${{ env.LOCAL_TAG }}"


def test_the_gate_scans_critical_and_high_and_never_fails_inside_trivy() -> None:
    action = load(ACTIONS / "vulnerability-gate" / "action.yml")
    scan = action["runs"]["steps"][0]
    assert scan["uses"].startswith("aquasecurity/trivy-action@")
    options = scan["with"]
    assert options["severity"] == "CRITICAL,HIGH"
    assert options["ignore-unfixed"] == "false"
    assert options["exit-code"] == "0"
    assert options["format"] == "json"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", options["version"])
    assert "evaluate.py" in action["runs"]["steps"][1]["run"]


def test_publish_central_runs_in_the_release_environment_after_publish() -> None:
    jobs = load(WORKFLOWS / "container-publish.yml")["jobs"]
    job = jobs["publish-central"]
    assert job["needs"] == "publish"
    assert job["environment"] == "spark-release"
    assert job["if"] == "github.event_name == 'push' && github.ref_type == 'tag'"
    assert job["permissions"] == {"id-token": "write", "contents": "read", "packages": "read"}
    assert job["env"] == {
        "AWS_REGION": "${{ vars.AWS_REGION }}",
        "AWS_RELEASE_ROLE_ARN": "${{ vars.AWS_RELEASE_ROLE_ARN }}",
        "CENTRAL_ECR_REPOSITORY_URI": "${{ vars.CENTRAL_ECR_REPOSITORY_URI }}",
        "IMAGE_SIGNING_KEY": "${{ vars.IMAGE_SIGNING_KEY }}",
    }
    steps = job["steps"]
    assert "::add-mask::" in steps[1]["run"], "identifiers are masked before anything else"
    central = next(s for s in steps if s.get("uses") == "./.github/actions/publish-central")
    assert central["with"]["role-to-assume"] == "${{ vars.AWS_RELEASE_ROLE_ARN }}"
    assert job["outputs"]["digest"] == "${{ steps.central.outputs.digest }}"


def test_deploy_test_waits_for_the_signed_central_image() -> None:
    jobs = load(WORKFLOWS / "container-publish.yml")["jobs"]
    job = jobs["deploy-test"]
    assert set(job["needs"]) == {"publish", "publish-central"}
    assert job["environment"] == "spark-test"
    deploy = next(s for s in job["steps"] if s.get("name") == "Deploy")
    assert deploy["env"]["EXPECTED_DIGEST"] == "${{ needs.publish-central.outputs.digest }}"
    assert job["env"]["IMAGE_SIGNING_PUBLIC_KEY"] == "${{ vars.IMAGE_SIGNING_PUBLIC_KEY }}"
    assert job["env"]["CENTRAL_ECR_REPOSITORY_URI"] == "${{ vars.CENTRAL_ECR_REPOSITORY_URI }}"
    uses = [s.get("uses") for s in job["steps"]]
    assert "./.github/actions/install-crane" in uses
    assert "./.github/actions/install-cosign" in uses


def test_both_copies_use_the_same_crane() -> None:
    publish = (WORKFLOWS / "container-publish.yml").read_text(encoding="utf-8")
    central = load(ACTIONS / "publish-central" / "action.yml")
    assert "CRANE_SHA256" not in publish, "crane is installed by the shared action only"
    assert "./.github/actions/install-crane" in [s.get("uses") for s in central["runs"]["steps"]]
    crane = (ACTIONS / "install-crane" / "action.yml").read_text(encoding="utf-8")
    assert re.search(r'CRANE_SHA256: "[0-9a-f]{64}"', crane)
    assert "sha256sum --check --strict" in crane


def test_cosign_stays_on_v2_tag_based_signatures() -> None:
    action = load(ACTIONS / "install-cosign" / "action.yml")
    installer = action["runs"]["steps"][0]
    assert installer["uses"].startswith("sigstore/cosign-installer@")
    assert re.fullmatch(r"v2\.\d+\.\d+", installer["with"]["cosign-release"])
