"""scripts/deploy-test.sh against a stub ``aws`` and ``curl`` that record their calls.

Every run, whatever the scenario, asserts the standing rules (``_check_rules``): every
CreateChangeSet reuses the template, runs as the execution role, lists the two resource
types and passes UsePreviousValue for every parameter but the one it changes; RunTask uses
only the stack's outputs; no task definition is registered, no service updated and the stack
never updated outside a change set; and no identifier appears in the log or the job summary
except in an ``::add-mask::`` line.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.scripts import aws_stub

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
]


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


def _shim(directory: Path, tool: str) -> None:
    shim = directory / tool
    python = Path(shutil.which("python") or "python").resolve().as_posix()
    stub = (HERE / "aws_stub.py").as_posix()
    shim.write_text(f'#!/usr/bin/env bash\nexec "{python}" "{stub}" {tool} "$@"\n', newline="\n")
    shim.chmod(0o755)


def deploy(
    tmp_path: Path, version: str = VERSION, env: dict[str, str] | None = None, **state: Any
) -> Run:
    """Run the script against a stack in ``state`` (see ``aws_stub.initial_state``)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _shim(bin_dir, "aws")
    _shim(bin_dir, "curl")
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(aws_stub.initial_state(**state)), encoding="utf-8")
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
        **(env or {}),
    }
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

    masked = "\n".join(line for line in run.output.splitlines() if line.startswith("::add-mask::"))
    visible = "\n".join(
        line for line in run.output.splitlines() if not line.startswith("::add-mask::")
    )
    for identifier in IDENTIFIERS:
        assert identifier not in visible, f"{identifier} is printed unmasked"
        assert identifier not in run.summary, f"{identifier} is in the job summary"
    if run.returncode == 2:  # refused before reading anything
        return
    always = [aws_stub.ACCOUNT, STACK_NAME, ROLE_NAME]
    after_outputs = [aws_stub.CLUSTER, *aws_stub.SUBNETS, aws_stub.SECURITY_GROUP]
    after_outputs += [aws_stub.WEB_SERVICE, aws_stub.WORKER_SERVICE]
    read_outputs = run.count("cloudformation", "describe-stacks") > 1
    expected = always + (after_outputs if read_outputs else [])
    if run.count("ecs", "run-task") and not run.state["run_task_fails"]:
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
    assert run.count("ecs", "describe-services") > 0
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
    assert run.count("ecs", "describe-services") == 0


def test_a_migrate_task_that_cannot_start_stops_before_the_services(tmp_path: Path) -> None:
    run = deploy(tmp_path, run_task_fails=True)
    assert run.returncode != 0
    assert "RESOURCE:CPU" in run.output
    assert run.count("cloudformation", "create-change-set") == 1
    assert run.state["params"]["ImageTag"] == "0.3.0rc16"


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
    assert run.count("ecs", "describe-services") == 0
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
