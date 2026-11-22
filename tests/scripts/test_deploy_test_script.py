"""scripts/deploy-test.sh against a stub ``aws`` and ``curl`` that record their calls.

Every run, whatever the scenario, asserts the standing rules (``_check_rules``): every
CreateChangeSet reuses the template, runs as the execution role, lists the two resource
types and passes UsePreviousValue for every parameter but the one it changes; RunTask uses
only the stack's outputs; no task definition is registered, no service updated and the stack
never updated outside a change set; no identifier appears in the log or the job summary
except in an ``::add-mask::`` line; and no AWS call is made before the image is verified: the
test copy's digest is read, and a signature by the release key on that digest in the central
repository is accepted (``crane`` and ``cosign`` are tests/scripts/registry_stub.py); and
the migrate task is started with ECS Exec off.

The hardening check (step 1a) reads the stack's task definitions and services; its tests are
at the end of this file.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from tests.scripts import aws_stub
from tests.scripts import image_helpers as images

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent.parent / "scripts" / "deploy-test.sh"
VERSION = "0.3.0rc17"
STACK_NAME = "example-service-stack"
ROLE_NAME = "example-service-deploy"
EXECUTION_ROLE_ARN = f"arn:aws:iam::{aws_stub.ACCOUNT}:role/{ROLE_NAME}"
RESOURCE_TYPES = ["AWS::ECS::TaskDefinition", "AWS::ECS::Service"]
FORBIDDEN_CALLS = {
    ("ecs", "register-task-definition"),
    ("ecs", "update-service"),
    ("cloudformation", "update-stack"),
    ("cloudformation", "create-stack"),
    ("cloudformation", "delete-stack"),
}
IDENTIFIERS = [
    aws_stub.ACCOUNT,
    aws_stub.CLUSTER,
    *aws_stub.SUBNETS,
    aws_stub.SECURITY_GROUP,
    aws_stub.WEB_SERVICE,
    aws_stub.WORKER_SERVICE,
    aws_stub.TASK_ID,
    STACK_NAME,
    ROLE_NAME,
    images.TEST_HOST,
    images.CENTRAL_HOST,
    images.CENTRAL_ACCOUNT,
    aws_stub.FILE_SYSTEM,
    "example-migrate",
]
TEST_COPY = f"{images.TEST_URI}:{VERSION}"
CENTRAL = f"{images.CENTRAL_URI}:{VERSION}"


def published(
    test_digest: str = images.DIGEST_A,
    central_digest: str | None = None,
    signer: str | None = "release",
) -> dict[str, Any]:
    """The registries after publish-central and the test copy: both tags, one signature."""
    model = images.registry()
    model["tags"][TEST_COPY] = test_digest
    model["tags"][CENTRAL] = central_digest or test_digest
    if signer:
        images.signed(model, images.CENTRAL_URI, central_digest or test_digest, signer)
    return model


@dataclass
class Run:
    returncode: int
    output: str
    summary: str
    calls: list[list[str]]
    state: dict[str, Any]

    def names(self) -> list[tuple[str, str]]:
        return [(c[1], c[2]) for c in self.calls if c[0] == "aws" and len(c) > 2]

    def count(self, service: str, command: str) -> int:
        return self.names().count((service, command))

    def service_checks(self) -> int:
        """The step 5 DescribeServices calls (not the hardening check's ECS Exec reads)."""
        return sum(
            1
            for c in self.calls
            if c[:3] == ["aws", "ecs", "describe-services"]
            and aws_stub.option(c, "--query") != "services[0].enableExecuteCommand"
        )

    def cosign_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "cosign"]


def deploy(
    tmp_path: Path,
    version: str = VERSION,
    env: dict[str, str] | None = None,
    registry: dict[str, Any] | None = None,
    **state: Any,
) -> Run:
    """Run the script against a stack in ``state`` (see ``aws_stub.initial_state``) and the
    registries in ``registry`` (by default published and signed by the release key)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("aws", "curl"):
        images.shim(bin_dir, tool, "aws_stub.py")
    for tool in ("crane", "cosign"):
        images.shim(bin_dir, tool, "registry_stub.py")
    state_path = tmp_path / "state.json"
    full_state = aws_stub.initial_state(**state)
    full_state["registry"] = registry if registry is not None else published()
    state_path.write_text(json.dumps(full_state), encoding="utf-8")
    summary_path = tmp_path / "summary.md"
    environment = {
        **os.environ,
        "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
        "STUB_STATE": state_path.as_posix(),
        "STACK_NAME": STACK_NAME,
        "EXECUTION_ROLE_ARN": EXECUTION_ROLE_ARN,
        "AWS_REGION": aws_stub.REGION,
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_STEP_SUMMARY": summary_path.as_posix(),
        "POLL_INTERVAL": "0",
        "READY_TIMEOUT": "0",
        "TASK_TIMEOUT": "30",
        "ECR_REPOSITORY_URI": images.TEST_URI,
        "CENTRAL_ECR_REPOSITORY_URI": images.CENTRAL_URI,
        "IMAGE_SIGNING_PUBLIC_KEY": images.RELEASE_PEM,
    }
    environment.pop("GITHUB_ACTIONS", None)
    environment.pop("EXPECTED_DIGEST", None)
    environment.pop("HARDENING_CHECK", None)
    environment.update(env or {})
    bash = shutil.which("bash")
    assert bash, "bash is required"
    result = subprocess.run(
        [bash, SCRIPT.as_posix(), version],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    final = json.loads(state_path.read_text(encoding="utf-8"))
    run = Run(
        returncode=result.returncode,
        output=result.stdout + result.stderr,
        summary=summary_path.read_text(encoding="utf-8") if summary_path.exists() else "",
        calls=final["calls"],
        state=final,
    )
    _check_rules(run, initial=aws_stub.initial_state(**state))
    return run


def _check_rules(run: Run, initial: dict[str, Any]) -> None:
    for service, command in run.names():
        assert (service, command) not in FORBIDDEN_CALLS, f"forbidden call {service} {command}"

    parameter_keys = list(initial["params"])
    for call in (c for c in run.calls if c[:3] == ["aws", "cloudformation", "create-change-set"]):
        assert "--use-previous-template" in call
        assert "--template-body" not in call and "--template-url" not in call
        assert "--capabilities" not in call
        assert call[call.index("--role-arn") + 1] == EXECUTION_ROLE_ARN
        types = aws_stub.option_values(call, "--resource-types")
        assert types == RESOURCE_TYPES
        parameters = aws_stub.option_values(call, "--parameters")
        assert [p.split(",")[0] for p in parameters] == [
            f"ParameterKey={k}" for k in parameter_keys
        ]
        changed = [p for p in parameters if "ParameterValue=" in p]
        assert len(changed) == 1, "exactly one parameter is set"
        assert all(p.endswith(",UsePreviousValue=true") for p in parameters if p not in changed)
        assert re.fullmatch(r"ParameterKey=(Migrate)?ImageTag,ParameterValue=\S+", changed[0])
        name = call[call.index("--change-set-name") + 1]
        assert re.fullmatch(r"spark-deploy-[A-Za-z0-9-]+", name) and name[0].isalpha()

    for call in (c for c in run.calls if c[:3] == ["aws", "ecs", "run-task"]):
        outputs = {o["OutputKey"]: o["OutputValue"] for o in aws_stub.outputs(initial)}
        arn = call[call.index("--task-definition") + 1]
        assert re.fullmatch(
            rf"arn:aws:ecs:{aws_stub.REGION}:{aws_stub.ACCOUNT}:task-definition/example-migrate:\d+",
            arn,
        )
        assert call[call.index("--cluster") + 1] == outputs["ClusterName"]
        assert call[call.index("--launch-type") + 1] == "FARGATE"
        network = call[call.index("--network-configuration") + 1]
        assert network == (
            f"awsvpcConfiguration={{subnets=[{outputs['MigrateSubnetIds']}],"
            f"securityGroups=[{outputs['MigrateSecurityGroupId']}],assignPublicIp=DISABLED}}"
        )
        assert "--overrides" not in call and "--platform-version" not in call
        assert "--disable-execute-command" in call
        assert "--enable-execute-command" not in call
        assert "--no-enable-execute-command" not in call

    masked = "\n".join(line for line in run.output.splitlines() if line.startswith("::add-mask::"))
    visible = "\n".join(
        line for line in run.output.splitlines() if not line.startswith("::add-mask::")
    )
    for identifier in IDENTIFIERS:
        assert identifier not in visible, f"{identifier} is printed unmasked"
        assert identifier not in run.summary, f"{identifier} is in the job summary"
    if run.returncode == 2:  # refused before reading anything
        return

    # No AWS call before the image is verified, on every path.
    aws_calls = [n for n, c in enumerate(run.calls) if c[0] == "aws"]
    if aws_calls:
        before = run.calls[: aws_calls[0]]
        assert ["crane", "digest", TEST_COPY] in before, "the test copy was not read first"
        verifies = [c for c in before if c[:2] == ["cosign", "verify"]]
        assert verifies, "AWS was called before the image was verified"
        test_digest = run.state["registry"]["tags"][TEST_COPY]
        assert verifies[-1][-1] == f"{images.CENTRAL_URI}@{test_digest}"
        assert "--insecure-ignore-tlog=true" in verifies[-1]
    always = [aws_stub.ACCOUNT, STACK_NAME, ROLE_NAME]
    always += [images.TEST_HOST, images.CENTRAL_HOST, images.CENTRAL_ACCOUNT]
    after_outputs = [aws_stub.CLUSTER, *aws_stub.SUBNETS, aws_stub.SECURITY_GROUP]
    after_outputs += [aws_stub.WEB_SERVICE, aws_stub.WORKER_SERVICE]
    read_outputs = run.count("cloudformation", "describe-stacks") > 1
    expected = always + (after_outputs if read_outputs else [])
    started = not run.state["run_task_fails"] and not run.state["deny_run_task"]
    if run.count("ecs", "run-task") and started:
        expected.append(aws_stub.TASK_ID)
    for identifier in expected:
        assert identifier in masked, f"{identifier} was never masked"
    assert not re.search(r"arn:aws[a-z-]*:\S*\d{12}", visible), "an ARN is printed"


def _first_index(run: Run, service: str, command: str) -> int:
    return run.names().index((service, command))


# ---- the normal paths -------------------------------------------------------------


def test_a_normal_deploy_runs_two_change_sets_with_the_migrate_task_between(tmp_path: Path) -> None:
    run = deploy(tmp_path)
    assert run.returncode == 0, run.output
    created = [c for c in run.calls if c[:3] == ["aws", "cloudformation", "create-change-set"]]
    assert [c[c.index("--change-set-name") + 1] for c in created] == [
        "spark-deploy-migrate-0-3-0rc17-123-1",
        "spark-deploy-0-3-0rc17-123-1",
    ]
    order = [name for name in run.names() if name[1] in {"create-change-set", "run-task"}]
    assert order == [
        ("cloudformation", "create-change-set"),
        ("ecs", "run-task"),
        ("cloudformation", "create-change-set"),
    ]
    first_run_task = _first_index(run, "ecs", "run-task")
    executes = [
        i for i, n in enumerate(run.names()) if n == ("cloudformation", "execute-change-set")
    ]
    assert len(executes) == 2 and executes[0] < first_run_task < executes[1]
    assert run.state["params"]["ImageTag"] == VERSION
    assert run.state["params"]["MigrateImageTag"] == VERSION
    assert "migrate task finished with exit code 0" in run.output
    assert "check: /ready answered 200" in run.output
    assert "Deploy of `0.3.0rc17` succeeded." in run.summary


def test_the_first_deploy_from_none_starts_the_services(tmp_path: Path) -> None:
    run = deploy(
        tmp_path,
        params={
            **aws_stub.initial_state()["params"],
            "ImageTag": "none",
            "MigrateImageTag": "none",
        },
    )
    assert run.returncode == 0, run.output
    assert run.count("ecs", "run-task") == 1
    assert run.state["params"]["ImageTag"] == VERSION
    assert "1 of 1 running" in run.output


def test_a_rerun_with_the_version_already_running_only_checks(tmp_path: Path) -> None:
    params = {**aws_stub.initial_state()["params"], "ImageTag": VERSION, "MigrateImageTag": VERSION}
    run = deploy(tmp_path, params=params)
    assert run.returncode == 0, run.output
    assert run.count("cloudformation", "create-change-set") == 0
    assert run.count("ecs", "run-task") == 0
    assert run.service_checks() > 0
    assert "going to the checks" in run.output


def test_a_rerun_after_the_migrate_change_set_skips_it_and_still_migrates(tmp_path: Path) -> None:
    params = {**aws_stub.initial_state()["params"], "MigrateImageTag": VERSION}
    run = deploy(tmp_path, params=params)
    assert run.returncode == 0, run.output
    created = [c for c in run.calls if c[:3] == ["aws", "cloudformation", "create-change-set"]]
    assert [c[c.index("--change-set-name") + 1] for c in created] == [
        "spark-deploy-0-3-0rc17-123-1"
    ]
    assert run.count("ecs", "run-task") == 1


def test_a_stack_that_rolled_back_earlier_may_be_deployed_to(tmp_path: Path) -> None:
    run = deploy(tmp_path, status="UPDATE_ROLLBACK_COMPLETE")
    assert run.returncode == 0, run.output


@pytest.mark.parametrize(
    "status", ["UPDATE_ROLLBACK_FAILED", "UPDATE_IN_PROGRESS", "CREATE_FAILED"]
)
def test_a_stack_in_another_state_is_refused_before_any_change(tmp_path: Path, status: str) -> None:
    run = deploy(tmp_path, status=status)
    assert run.returncode != 0
    assert status in run.output
    assert run.count("cloudformation", "create-change-set") == 0
    assert run.count("ecs", "run-task") == 0


# ---- change sets ------------------------------------------------------------------


def test_change_sets_with_no_changes_are_deleted_and_the_checks_decide(tmp_path: Path) -> None:
    run = deploy(tmp_path, no_changes=True)
    # Nothing changed, so RunningImageTag is not the version and the checks fail.
    assert run.returncode != 0
    assert run.count("cloudformation", "delete-change-set") == 2
    assert run.count("cloudformation", "execute-change-set") == 0
    assert "RunningImageTag" in run.output


@pytest.mark.parametrize(
    "extra",
    [
        ["Modify", "Database", "AWS::RDS::DBInstance"],
        ["Add", "ExtraService", "AWS::ECS::Service"],
        ["Remove", "WebService", "AWS::ECS::Service"],
        ["Modify", "WebService", "AWS::ECS::Service"],
    ],
)
def test_an_unexpected_change_in_the_migrate_change_set_is_deleted_and_fails(
    tmp_path: Path, extra: list[str]
) -> None:
    run = deploy(tmp_path, extra_change=extra, extra_change_on="MigrateImageTag")
    assert run.returncode != 0
    assert run.count("cloudformation", "delete-change-set") == 1
    assert run.count("cloudformation", "execute-change-set") == 0
    assert run.count("ecs", "run-task") == 0
    assert f"{extra[0]} {extra[1]}" in run.output


@pytest.mark.parametrize(
    "extra",
    [
        ["Modify", "Database", "AWS::RDS::DBInstance"],
        ["Remove", "WebService", "AWS::ECS::Service"],
        ["Modify", "MigrateTaskDefinition", "AWS::ECS::TaskDefinition"],
    ],
)
def test_an_unexpected_change_in_the_service_change_set_is_deleted_and_fails(
    tmp_path: Path, extra: list[str]
) -> None:
    run = deploy(tmp_path, extra_change=extra, extra_change_on="ImageTag")
    assert run.returncode != 0
    assert run.count("cloudformation", "execute-change-set") == 1  # only the migrate change set
    assert run.count("cloudformation", "delete-change-set") == 1
    assert run.state["params"]["ImageTag"] == "0.3.0rc16"


def test_create_change_set_refused_fails_with_the_message_and_no_arns(tmp_path: Path) -> None:
    run = deploy(tmp_path, deny_create_change_set=True)
    assert run.returncode != 0
    assert "AccessDenied" in run.output
    assert "is not authorized to perform: cloudformation:CreateChangeSet" in run.output
    assert "arn:***" in run.output
    assert run.count("cloudformation", "execute-change-set") == 0
    assert run.count("ecs", "run-task") == 0


# ---- the migrate task -------------------------------------------------------------


def test_a_migrate_task_that_fails_stops_before_the_services(tmp_path: Path) -> None:
    run = deploy(tmp_path, migrate_exit_code=1, migrate_stopped_reason="Essential container exited")
    assert run.returncode != 0
    assert "exit code: 1" in run.output
    assert "Essential container exited" in run.output
    created = [c for c in run.calls if c[:3] == ["aws", "cloudformation", "create-change-set"]]
    assert len(created) == 1  # the migrate change set only
    assert run.count("cloudformation", "execute-change-set") == 1
    assert run.state["params"]["ImageTag"] == "0.3.0rc16"
    assert run.service_checks() == 0


def test_a_migrate_task_that_cannot_start_stops_before_the_services(tmp_path: Path) -> None:
    run = deploy(tmp_path, run_task_fails=True)
    assert run.returncode != 0
    assert "RESOURCE:CPU" in run.output
    assert run.count("cloudformation", "create-change-set") == 1
    assert run.state["params"]["ImageTag"] == "0.3.0rc16"


def test_a_refused_run_task_shows_the_scrubbed_message_and_leaves_the_services_alone(
    tmp_path: Path,
) -> None:
    # aws_text runs inside run_out="$(...)": its messages must reach the log, not the variable.
    run = deploy(tmp_path, deny_run_task=True)
    assert run.returncode != 0
    assert "AWS refused or failed a call (aws ecs run-task):" in run.output
    assert "AccessDeniedException" in run.output
    assert "is not authorized to perform: ecs:RunTask" in run.output
    assert "::error::the call aws ecs run-task failed; see the message above" in run.output
    assert "arn:***" in run.output
    visible = [line for line in run.output.splitlines() if not line.startswith("::add-mask::")]
    assert aws_stub.ACCOUNT not in chr(10).join(visible)
    assert "Deploy FAILED: the call aws ecs run-task failed" in run.summary
    assert run.count("ecs", "describe-tasks") == 0
    assert run.count("cloudformation", "execute-change-set") == 1  # the migrate change set only
    assert run.state["params"]["ImageTag"] == "0.3.0rc16"
    assert run.service_checks() == 0


@pytest.mark.parametrize("option", ["--no-enable-execute-command", "--enable-execute-command"])
def test_the_stub_refuses_the_options_the_cli_renames(tmp_path: Path, option: str) -> None:
    # What let --no-enable-execute-command through to a real run: the stub now refuses it.
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(aws_stub.initial_state()), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(aws_stub.__file__)), "aws", "ecs", "run-task", option],
        env={**os.environ, "STUB_STATE": state_path.as_posix()},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 252
    assert f"Unknown options: {option}" in result.stderr


# ---- the stack update -------------------------------------------------------------


def test_a_stack_update_that_rolls_back_fails_with_the_events_and_is_not_repaired(
    tmp_path: Path,
) -> None:
    run = deploy(tmp_path, rollback_on="ImageTag")
    assert run.returncode != 0
    assert "UPDATE_ROLLBACK_COMPLETE" in run.output
    assert "ECS Deployment Circuit Breaker" in run.output
    assert run.count("cloudformation", "describe-stack-events") == 1
    assert run.count("cloudformation", "execute-change-set") == 2
    assert run.service_checks() == 0
    assert run.state["params"]["ImageTag"] == "0.3.0rc16"


# ---- the checks -------------------------------------------------------------------


@pytest.mark.parametrize("service", ["web", "worker"])
def test_a_service_not_on_the_stacks_task_definition_fails(tmp_path: Path, service: str) -> None:
    run = deploy(tmp_path, service_wrong_taskdef=service)
    assert run.returncode != 0
    assert f"the {service} service's primary deployment is not on the stack's task definition" in (
        run.output
    )


def test_a_service_that_has_not_finished_rolling_out_fails(tmp_path: Path) -> None:
    run = deploy(tmp_path, rollout_state="IN_PROGRESS", env={"SERVICE_TIMEOUT": "0"})
    assert run.returncode != 0
    assert "not settled: rollout IN_PROGRESS" in run.output


@pytest.mark.parametrize("failure", [{"ready_code": 503}, {"ready_version": "0.3.0rc16"}])
def test_ready_must_answer_200_with_the_version(tmp_path: Path, failure: dict[str, Any]) -> None:
    run = deploy(tmp_path, **failure)
    assert run.returncode != 0
    assert "/ready did not answer 200" in run.output


def test_a_bad_version_is_refused(tmp_path: Path) -> None:
    run = deploy(tmp_path, version="bad version;rm")
    assert run.returncode == 2
    assert run.calls == []


# ---- verification -----------------------------------------------------------------


def test_a_deploy_summarises_the_verified_digest(tmp_path: Path) -> None:
    run = deploy(tmp_path)
    assert run.returncode == 0, run.output
    assert f"Image: `{images.DIGEST_A}`, verified as signed by the release key." in run.summary


@pytest.mark.parametrize(
    "registry",
    [
        pytest.param(published(signer=None), id="unsigned"),
        pytest.param(published(signer="other"), id="signed-by-another-key"),
        pytest.param(
            published(images.DIGEST_A, images.DIGEST_B), id="test-copy-is-not-the-signed-digest"
        ),
        pytest.param(
            {**published(), "claim_digest": images.DIGEST_C}, id="signature-names-another-digest"
        ),
    ],
)
def test_an_image_that_does_not_verify_changes_nothing(
    tmp_path: Path, registry: dict[str, Any]
) -> None:
    run = deploy(tmp_path, registry=registry)
    assert run.returncode == 1
    assert run.names() == [], "AWS was called"
    assert "nothing was changed" in run.output
    assert "Deploy FAILED" in run.summary


def test_a_rerun_also_verifies_first(tmp_path: Path) -> None:
    params = {**aws_stub.initial_state()["params"], "ImageTag": VERSION, "MigrateImageTag": VERSION}
    run = deploy(tmp_path, params=params, registry=published(signer=None))
    assert run.returncode == 1
    assert run.names() == []


def test_a_missing_test_copy_changes_nothing(tmp_path: Path) -> None:
    registry = published()
    del registry["tags"][TEST_COPY]
    run = deploy(tmp_path, registry=registry)
    assert run.returncode == 1
    assert run.names() == []
    assert run.cosign_calls() == []


def test_the_digest_publish_central_signed_must_be_the_test_copy(tmp_path: Path) -> None:
    run = deploy(tmp_path, env={"EXPECTED_DIGEST": images.DIGEST_B})
    assert run.returncode == 1
    assert run.names() == []
    assert run.cosign_calls() == []
    assert "not the digest publish-central signed" in run.output


def test_the_digest_publish_central_signed_is_accepted(tmp_path: Path) -> None:
    run = deploy(tmp_path, env={"EXPECTED_DIGEST": images.DIGEST_A})
    assert run.returncode == 0, run.output


@pytest.mark.parametrize(
    "name", ["IMAGE_SIGNING_PUBLIC_KEY", "CENTRAL_ECR_REPOSITORY_URI", "ECR_REPOSITORY_URI"]
)
def test_the_verification_settings_are_required(tmp_path: Path, name: str) -> None:
    run = deploy(tmp_path, env={name: ""})
    assert run.returncode == 2
    assert run.calls == []
    assert f"{name} is not set" in run.output


# ---- the hardening check ----------------------------------------------------------

ENFORCE = {"HARDENING_CHECK": "enforce"}
LOGICAL_IDS = {
    "migrate": "MigrateTaskDefinition",
    "web": "WebTaskDefinition",
    "worker": "WorkerTaskDefinition",
}
DOCS = HERE.parent.parent / "docs" / "deployment"
Change = Callable[[dict[str, Any]], None]


def task_definitions(role: str = "web", change: Change | None = None) -> dict[str, Any]:
    """The stub's task definitions, with ``change(task_definition)`` applied to one role's."""
    definitions = {
        logical_id: aws_stub.hardened_task_definition(logical_id)
        for logical_id in aws_stub.TASK_DEFINITIONS
    }
    if change is not None:
        change(definitions[LOGICAL_IDS[role]])
    return definitions


def spark(task_definition: dict[str, Any]) -> dict[str, Any]:
    container: dict[str, Any] = task_definition["containerDefinitions"][0]
    return container


def tmp_volume(task_definition: dict[str, Any]) -> dict[str, Any]:
    volume: dict[str, Any] = next(v for v in task_definition["volumes"] if v["name"] == "spark-tmp")
    return volume


def _set(path: list[str], value: Any) -> Change:
    def change(task_definition: dict[str, Any]) -> None:
        target = spark(task_definition)
        for key in path[:-1]:
            target = target.setdefault(key, {})
        target[path[-1]] = value

    return change


def _drop(path: list[str]) -> Change:
    def change(task_definition: dict[str, Any]) -> None:
        target = spark(task_definition)
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return change


def _no_tmp_mount(task_definition: dict[str, Any]) -> None:
    container = spark(task_definition)
    container["mountPoints"] = [m for m in container["mountPoints"] if m["containerPath"] != "/tmp"]


def _tmp_read_only(task_definition: dict[str, Any]) -> None:
    for mount in spark(task_definition)["mountPoints"]:
        if mount["containerPath"] == "/tmp":
            mount["readOnly"] = True


def _tmp_on_a_host_path(task_definition: dict[str, Any]) -> None:
    tmp_volume(task_definition)["host"] = {"sourcePath": "/var/tmp"}


def _tmp_on_efs(task_definition: dict[str, Any]) -> None:
    volume = tmp_volume(task_definition)
    volume.pop("host", None)
    volume["efsVolumeConfiguration"] = {"fileSystemId": aws_stub.FILE_SYSTEM}


def _tmp_volume_undeclared(task_definition: dict[str, Any]) -> None:
    task_definition["volumes"] = [v for v in task_definition["volumes"] if v["name"] != "spark-tmp"]


def _not_the_spark_image(task_definition: dict[str, Any]) -> None:
    spark(task_definition)["image"] = "public.ecr.aws/example/other:1"


MISSING_CONTROLS = [
    pytest.param(_drop(["user"]), "does not run as user 10001:10001", id="no-user"),
    pytest.param(_set(["user"], "0"), "does not run as user 10001:10001", id="root-user"),
    pytest.param(
        _drop(["readonlyRootFilesystem"]), "does not have readonlyRootFilesystem true", id="no-ro"
    ),
    pytest.param(
        _set(["readonlyRootFilesystem"], False),
        "does not have readonlyRootFilesystem true",
        id="ro-false",
    ),
    pytest.param(_set(["privileged"], True), "is privileged", id="privileged"),
    pytest.param(
        _drop(["linuxParameters", "capabilities", "drop"]),
        "does not drop ALL capabilities",
        id="no-drop",
    ),
    pytest.param(
        _set(["linuxParameters", "capabilities", "drop"], ["NET_RAW"]),
        "does not drop ALL capabilities",
        id="drop-some",
    ),
    pytest.param(
        _set(["linuxParameters", "capabilities", "add"], ["SYS_PTRACE"]),
        "adds capabilities (SYS_PTRACE)",
        id="adds",
    ),
    pytest.param(
        _drop(["linuxParameters", "initProcessEnabled"]),
        "does not have linuxParameters.initProcessEnabled true",
        id="no-init",
    ),
    pytest.param(
        _set(["linuxParameters", "initProcessEnabled"], False),
        "does not have linuxParameters.initProcessEnabled true",
        id="init-false",
    ),
    pytest.param(
        _drop(["linuxParameters"]), "does not drop ALL capabilities", id="no-linux-parameters"
    ),
    pytest.param(_no_tmp_mount, "mounts no task volume at /tmp", id="no-tmp"),
    pytest.param(_tmp_read_only, "mounts /tmp read-only", id="tmp-read-only"),
    pytest.param(_tmp_on_a_host_path, "mounts /tmp from a host path", id="tmp-host-path"),
    pytest.param(_tmp_on_efs, "mounts /tmp from EFS, Docker or FSx storage", id="tmp-efs"),
    pytest.param(
        _tmp_volume_undeclared,
        "mounts /tmp from a volume the task definition does not declare",
        id="tmp-undeclared",
    ),
    pytest.param(
        _not_the_spark_image,
        "the web task definition has no container running the Spark image",
        id="no-spark-container",
    ),
]


def _refused_before_any_change(run: Run, note: str = "enforced") -> None:
    assert run.returncode == 1, run.output
    assert run.count("cloudformation", "create-change-set") == 0
    assert run.count("cloudformation", "execute-change-set") == 0
    assert run.count("ecs", "run-task") == 0
    assert run.state["params"]["ImageTag"] == "0.3.0rc16"
    assert f"Hardening check: FAILED ({note}), nothing was changed:" in run.summary
    assert "Deploy FAILED: the test stack does not carry the container hardening" in run.summary
    assert "::error::hardening: " in run.output


def test_a_hardened_stack_passes_the_check(tmp_path: Path) -> None:
    run = deploy(tmp_path, env=ENFORCE)
    assert run.returncode == 0, run.output
    assert run.count("ecs", "describe-task-definition") == 6  # three definitions, two reads each
    assert "check: hardening: every Spark container" in run.output
    assert "Hardening check: passed (enforced)." in run.summary
    assert "::warning::" not in run.output and "::error::" not in run.output


def test_the_check_only_reads_and_comes_before_any_change(tmp_path: Path) -> None:
    run = deploy(tmp_path, env=ENFORCE)
    names = run.names()
    last_read = max(i for i, n in enumerate(names) if n == ("ecs", "describe-task-definition"))
    assert last_read < _first_index(run, "cloudformation", "create-change-set")
    assert set(names[: last_read + 1]) <= {
        ("cloudformation", "describe-stacks"),
        ("cloudformation", "describe-stack-resources"),
        ("ecs", "describe-task-definition"),
        ("ecs", "describe-services"),
    }


@pytest.mark.parametrize("change, message", MISSING_CONTROLS)
def test_each_missing_control_is_refused_before_any_change(
    tmp_path: Path, change: Change, message: str
) -> None:
    run = deploy(tmp_path, env=ENFORCE, task_definitions=task_definitions("web", change))
    _refused_before_any_change(run)
    assert message in run.output
    assert message in run.summary


@pytest.mark.parametrize("role", ["migrate", "web", "worker"])
def test_every_task_definition_is_checked(tmp_path: Path, role: str) -> None:
    change = _set(["readonlyRootFilesystem"], False)
    run = deploy(tmp_path, env=ENFORCE, task_definitions=task_definitions(role, change))
    _refused_before_any_change(run)
    container = aws_stub.CONTAINER_NAMES[LOGICAL_IDS[role]]
    assert (
        f"the {role} task's container {container} does not have readonlyRootFilesystem true"
        in run.output
    )


@pytest.mark.parametrize("service", ["web", "worker"])
def test_a_service_with_ecs_exec_on_is_refused(tmp_path: Path, service: str) -> None:
    exec_enabled = {"web": False, "worker": False, service: True}
    run = deploy(tmp_path, env=ENFORCE, exec_enabled=exec_enabled)
    _refused_before_any_change(run)
    assert f"the {service} service does not have ECS Exec off" in run.output


def test_every_problem_is_listed_not_only_the_first(tmp_path: Path) -> None:
    definitions = task_definitions("web", _drop(["user"]))
    _set(["privileged"], True)(definitions["WorkerTaskDefinition"])
    run = deploy(
        tmp_path,
        env=ENFORCE,
        task_definitions=definitions,
        exec_enabled={"web": True, "worker": False},
    )
    _refused_before_any_change(run)
    assert "(3 problems, listed above" in run.output
    assert run.summary.count("\n- the ") == 3


def test_an_unreadable_task_definition_is_refused_and_names_nothing(tmp_path: Path) -> None:
    run = deploy(tmp_path, env=ENFORCE, deny_describe_task_definition=True)
    _refused_before_any_change(run)
    assert "the migrate task definition could not be read" in run.output
    assert "is not authorized to perform: ecs:DescribeTaskDefinition" in run.output
    assert "arn:***" in run.output


def test_a_sidecar_that_is_not_spark_is_not_checked(tmp_path: Path) -> None:
    def add_sidecar(task_definition: dict[str, Any]) -> None:
        task_definition["containerDefinitions"].append(
            {"name": "log-router", "image": "public.ecr.aws/example/log-router:1"}
        )

    run = deploy(tmp_path, env=ENFORCE, task_definitions=task_definitions("web", add_sidecar))
    assert run.returncode == 0, run.output
    assert "Hardening check: passed" in run.summary


def test_a_rerun_is_checked_too(tmp_path: Path) -> None:
    params = {**aws_stub.initial_state()["params"], "ImageTag": VERSION, "MigrateImageTag": VERSION}
    change = _drop(["readonlyRootFilesystem"])
    run = deploy(
        tmp_path, params=params, env=ENFORCE, task_definitions=task_definitions("worker", change)
    )
    assert run.returncode == 1, run.output
    assert "Hardening check: FAILED (enforced), nothing was changed:" in run.summary
    assert run.count("ecs", "describe-services") == 2  # the check's ECS Exec reads
    assert run.service_checks() == 0


def test_warn_mode_reports_every_problem_and_deploys(tmp_path: Path) -> None:
    run = deploy(
        tmp_path,
        env={"HARDENING_CHECK": "warn"},
        task_definitions=task_definitions("web", _no_tmp_mount),
        exec_enabled={"web": False, "worker": True},
    )
    assert run.returncode == 0, run.output
    assert "::warning::hardening: the web task's container web mounts no task volume" in run.output
    assert "::warning::hardening: the worker service does not have ECS Exec off" in run.output
    assert "Hardening check: NOT MET (warning only, not enforced); the deploy continues:" in (
        run.summary
    )
    assert "docs/deployment/container-hardening.md (ECS on Fargate)" in run.summary
    assert "Deploy of `0.3.0rc17` succeeded." in run.summary


def test_the_default_is_the_scripts_constant(tmp_path: Path) -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    default = re.search(r'^HARDENING_CHECK_DEFAULT="([^"]+)"$', text, re.M)
    assert default, "the script names its default"
    run = deploy(tmp_path, task_definitions=task_definitions("web", _drop(["user"])))
    if default.group(1) == "warn":
        assert run.returncode == 0, run.output
        assert "Hardening check: NOT MET (warning only, not enforced)" in run.summary
    else:
        assert run.returncode == 1, run.output
        assert "Hardening check: FAILED" in run.summary


def test_enforced_from_a_date_that_has_passed(tmp_path: Path) -> None:
    run = deploy(
        tmp_path,
        env={"HARDENING_CHECK": "enforce-from:2000-01-01"},
        task_definitions=task_definitions("migrate", _drop(["user"])),
    )
    _refused_before_any_change(run, note="enforced since 2000-01-01")


def test_a_warning_until_a_date_still_to_come(tmp_path: Path) -> None:
    run = deploy(
        tmp_path,
        env={"HARDENING_CHECK": "enforce-from:2999-12-31"},
        task_definitions=task_definitions("migrate", _drop(["user"])),
    )
    assert run.returncode == 0, run.output
    assert (
        "Hardening check: NOT MET (warning only until 2999-12-31, enforced from that date)"
        in run.summary
    )


@pytest.mark.parametrize("policy", ["strict", "enforce-from:31/12/2026", "enforce-from:"])
def test_an_unknown_policy_is_refused_before_anything(tmp_path: Path, policy: str) -> None:
    run = deploy(tmp_path, env={"HARDENING_CHECK": policy})
    assert run.returncode == 2
    assert run.calls == []
    assert "HARDENING_CHECK must be enforce, warn or enforce-from:YYYY-MM-DD" in run.output


def _json_blocks(path: Path) -> list[Any]:
    blocks = re.findall(r"```json\n(.*?)\n```", path.read_text(encoding="utf-8"), re.S)
    return [json.loads(block) for block in blocks]


def test_the_web_task_definition_in_the_aws_guide_passes_the_check(tmp_path: Path) -> None:
    (web,) = [b for b in _json_blocks(DOCS / "aws.md") if b.get("family") == "spark-web"]
    web["family"] = aws_stub.TASK_DEFINITIONS["WebTaskDefinition"]
    web["containerDefinitions"][0]["image"] = f"{aws_stub.SPARK_REPOSITORY}:0.3.0rc16"
    definitions = task_definitions()
    definitions["WebTaskDefinition"] = web
    run = deploy(tmp_path, env=ENFORCE, task_definitions=definitions)
    assert run.returncode == 0, run.output
    assert "Hardening check: passed" in run.summary


def test_the_hardening_guides_fragments_pass_the_check(tmp_path: Path) -> None:
    blocks = _json_blocks(DOCS / "container-hardening.md")
    (fragment,) = [b for b in blocks if "readonlyRootFilesystem" in b]
    (volume,) = [b for b in blocks if b.get("name") == "spark-tmp"]
    container = {
        "name": "web",
        "image": f"{aws_stub.SPARK_REPOSITORY}:0.3.0rc16",
        "mountPoints": [{"sourceVolume": "spark-data", "containerPath": "/var/lib/spark"}],
    }
    container.update({k: v for k, v in fragment.items() if k != "mountPoints"})
    container["mountPoints"] += fragment["mountPoints"]
    web = aws_stub.hardened_task_definition("WebTaskDefinition")
    web["containerDefinitions"] = [container]
    web["volumes"] = [v for v in web["volumes"] if v["name"] != "spark-tmp"] + [volume]
    definitions = task_definitions()
    definitions["WebTaskDefinition"] = web
    run = deploy(tmp_path, env=ENFORCE, task_definitions=definitions)
    assert run.returncode == 0, run.output


def test_the_stub_pulls_from_the_test_repository() -> None:
    assert aws_stub.SPARK_REPOSITORY == images.TEST_URI
