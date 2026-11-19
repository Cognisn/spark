"""A stand-in for the ``aws`` and ``curl`` commands, for testing scripts/deploy-test.sh.

Run as ``python aws_stub.py aws <args>`` or ``python aws_stub.py curl <args>`` (the tests
put shims named ``aws`` and ``curl`` on PATH that do this). It keeps a small model of the
service stack in the JSON file named by ``STUB_STATE``, applies change sets to it, answers
``--query`` with real JMESPath and ``--output text`` the way the AWS CLI prints it, and
appends every call to the ``calls`` list of the state file. All names are invented.

Anything it does not model (RegisterTaskDefinition, UpdateService, UpdateStack, ...) exits 2
with a message, and is still recorded, so a test can assert it was never called.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import jmespath

ACCOUNT = "123456789012"
REGION = "ap-southeast-2"
CLUSTER = "example-cluster"
SUBNETS = ["subnet-0aaa1111", "subnet-0bbb2222"]
SECURITY_GROUP = "sg-0ccc3333"
WEB_SERVICE = "example-web"
WORKER_SERVICE = "example-worker"
TASK_ID = "0123456789abcdef0123456789abcdef"

TASK_DEFINITIONS = {
    "MigrateTaskDefinition": "example-migrate",
    "WebTaskDefinition": "example-web",
    "WorkerTaskDefinition": "example-worker",
}
CHANGES_FOR = {
    "MigrateImageTag": [("Modify", "MigrateTaskDefinition", "AWS::ECS::TaskDefinition")],
    "ImageTag": [
        ("Modify", "WebTaskDefinition", "AWS::ECS::TaskDefinition"),
        ("Modify", "WorkerTaskDefinition", "AWS::ECS::TaskDefinition"),
        ("Modify", "WebService", "AWS::ECS::Service"),
        ("Modify", "WorkerService", "AWS::ECS::Service"),
    ],
}


def task_definition_arn(logical_id: str, revision: int) -> str:
    return (
        f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/{TASK_DEFINITIONS[logical_id]}:{revision}"
    )


def initial_state(**overrides: Any) -> dict[str, Any]:
    """A healthy stack running ``0.3.0rc16``; keyword arguments replace top-level keys."""
    state: dict[str, Any] = {
        "status": "UPDATE_COMPLETE",
        "params": {
            "ImageTag": "0.3.0rc16",
            "MigrateImageTag": "0.3.0rc16",
            "MigrateShellCommand": "spark migrate && spark bootstrap",
            "RdsCaBundlePath": "/etc/ssl/certs/rds-global-bundle.pem",
            "WebCount": "1",
            "WebCpu": "1024",
        },
        "revisions": {
            "MigrateTaskDefinition": 1,
            "WebTaskDefinition": 1,
            "WorkerTaskDefinition": 1,
        },
        "changesets": {},
        "executing": None,
        "polls_left": 0,
        "calls": [],
        "task_polls": 0,
        # behaviour switches
        "deny_create_change_set": False,
        "no_changes": False,
        "extra_change": None,  # [action, logical id, type] added to every change set
        "extra_change_on": None,  # limit it to the change set that changes this parameter
        "migrate_exit_code": 0,
        "migrate_stopped_reason": "Essential container in task exited",
        "run_task_fails": False,
        "rollback_on": None,  # a parameter key whose change set ends in UPDATE_ROLLBACK_COMPLETE
        "service_wrong_taskdef": None,  # "web" or "worker"
        "rollout_state": "COMPLETED",
        "ready_code": 200,
        "ready_version": None,
    }
    state.update(overrides)
    return state


def running_tag(state: dict[str, Any]) -> str:
    return str(state["params"]["ImageTag"])


def outputs(state: dict[str, Any]) -> list[dict[str, str]]:
    revisions = state["revisions"]
    values = {
        "RunningImageTag": running_tag(state),
        "MigrateImageTag": state["params"]["MigrateImageTag"],
        "MigrateTaskDefinitionArn": task_definition_arn(
            "MigrateTaskDefinition", revisions["MigrateTaskDefinition"]
        ),
        "ClusterName": CLUSTER,
        "MigrateSubnetIds": ",".join(SUBNETS),
        "MigrateSecurityGroupId": SECURITY_GROUP,
        "WebServiceName": WEB_SERVICE,
        "WorkerServiceName": WORKER_SERVICE,
    }
    return [{"OutputKey": k, "OutputValue": v} for k, v in values.items()]


def option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def option_values(args: list[str], name: str) -> list[str]:
    """The values after ``name`` up to the next ``--option``."""
    if name not in args:
        return []
    values: list[str] = []
    for item in args[args.index(name) + 1 :]:
        if item.startswith("--"):
            break
        values.append(item)
    return values


def text(value: Any) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        if value and all(isinstance(item, list) for item in value):
            return "\n".join("\t".join(text(x) for x in item) for item in value)
        return "\t".join(text(item) for item in value)
    return str(value)


def respond(args: list[str], response: Any) -> None:
    query = option(args, "--query")
    result = jmespath.search(query, response) if query else response
    if option(args, "--output") == "text":
        print(text(result))
    else:
        print(json.dumps(result))


def finish_update(state: dict[str, Any]) -> None:
    """The executing change set ends: apply it or roll it back."""
    execution = state["executing"]
    state["executing"] = None
    key = execution["key"]
    if state["rollback_on"] == key:
        state["status"] = "UPDATE_ROLLBACK_COMPLETE"
        return
    state["params"][key] = execution["value"]
    logical_ids = [change[1] for change in CHANGES_FOR[key]]
    for logical_id in logical_ids:
        if logical_id in state["revisions"]:
            state["revisions"][logical_id] += 1
    state["status"] = "UPDATE_COMPLETE"


def cloudformation(state: dict[str, Any], command: str, args: list[str]) -> None:
    if command == "describe-stacks":
        if state["executing"] is not None:
            if state["polls_left"] > 0:
                state["polls_left"] -= 1
            else:
                finish_update(state)
        stack = {
            "StackStatus": state["status"],
            "Parameters": [
                {"ParameterKey": k, "ParameterValue": v} for k, v in state["params"].items()
            ],
            "Outputs": outputs(state),
        }
        respond(args, {"Stacks": [stack]})
    elif command == "create-change-set":
        if state["deny_create_change_set"]:
            print(
                f"An error occurred (AccessDenied) when calling the CreateChangeSet operation: "
                f"User: arn:aws:sts::{ACCOUNT}:assumed-role/example-deploy/run is not authorized "
                f"to perform: cloudformation:CreateChangeSet on resource: "
                f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:stack/example-stack/guid",
                file=sys.stderr,
            )
            sys.exit(254)
        name = option(args, "--change-set-name") or ""
        changed = [
            item
            for item in option_values(args, "--parameters")
            if "ParameterValue=" in item
            and item.split("ParameterValue=", 1)[1]
            != state["params"].get(item.split(",")[0].split("=")[1])
        ]
        key = changed[0].split(",")[0].split("=")[1] if changed else ""
        value = changed[0].split("ParameterValue=", 1)[1] if changed else ""
        state["changesets"][name] = {"key": key, "value": value}
        print(
            json.dumps({"Id": f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/{name}/guid"})
        )
    elif command == "describe-change-set":
        change_set = state["changesets"][option(args, "--change-set-name") or ""]
        key = change_set["key"]
        changes = [] if state["no_changes"] or not key else list(CHANGES_FOR[key])
        extra = state["extra_change"]
        if extra and state["extra_change_on"] in (None, key):
            changes.append(tuple(extra))
        if changes:
            status, reason = "CREATE_COMPLETE", ""
        else:
            status, reason = "FAILED", "The submitted information didn't contain changes."
        respond(
            args,
            {
                "Status": status,
                "StatusReason": reason,
                "Changes": [
                    {
                        "Type": "Resource",
                        "ResourceChange": {
                            "Action": action,
                            "LogicalResourceId": logical_id,
                            "ResourceType": resource_type,
                        },
                    }
                    for action, logical_id, resource_type in changes
                ],
            },
        )
    elif command == "delete-change-set":
        state["changesets"].pop(option(args, "--change-set-name") or "", None)
        print("{}")
    elif command == "execute-change-set":
        name = option(args, "--change-set-name") or ""
        state["executing"] = dict(state["changesets"][name])
        state["status"] = "UPDATE_IN_PROGRESS"
        state["polls_left"] = 2
        print("{}")
    elif command == "describe-stack-events":
        respond(
            args,
            {
                "StackEvents": [
                    {
                        "LogicalResourceId": "WebService",
                        "ResourceStatus": "UPDATE_FAILED",
                        "ResourceStatusReason": (
                            "Resource handler returned message: ECS Deployment Circuit Breaker "
                            "was triggered (Service: arn:aws:ecs:"
                            f"{REGION}:{ACCOUNT}:service/{CLUSTER}/{WEB_SERVICE})"
                        ),
                    },
                    {
                        "LogicalResourceId": "Stack",
                        "ResourceStatus": "UPDATE_ROLLBACK_COMPLETE",
                        "ResourceStatusReason": None,
                    },
                ]
            },
        )
    elif command == "describe-stack-resources":
        logical_id = option(args, "--logical-resource-id") or ""
        arn = task_definition_arn(logical_id, state["revisions"][logical_id])
        respond(args, {"StackResources": [{"PhysicalResourceId": arn}]})
    else:
        unsupported(state, "cloudformation", command)


def ecs(state: dict[str, Any], command: str, args: list[str]) -> None:
    if command == "run-task":
        if state["run_task_fails"]:
            respond(args, {"tasks": [], "failures": [{"reason": "RESOURCE:CPU"}]})
            return
        respond(
            args,
            {
                "tasks": [{"taskArn": f"arn:aws:ecs:{REGION}:{ACCOUNT}:task/{CLUSTER}/{TASK_ID}"}],
                "failures": [],
            },
        )
    elif command == "describe-tasks":
        state["task_polls"] += 1
        stopped = state["task_polls"] > 1
        respond(
            args,
            {
                "tasks": [
                    {
                        "lastStatus": "STOPPED" if stopped else "RUNNING",
                        "stoppedReason": state["migrate_stopped_reason"],
                        "containers": [
                            {"name": "sidecar", "exitCode": 0},
                            {"name": "spark-migrate", "exitCode": state["migrate_exit_code"]},
                        ],
                    }
                ]
            },
        )
    elif command == "describe-services":
        service = option(args, "--services") or ""
        which = "web" if service == WEB_SERVICE else "worker"
        logical_id = "WebTaskDefinition" if which == "web" else "WorkerTaskDefinition"
        revision = state["revisions"][logical_id]
        if state["service_wrong_taskdef"] == which:
            revision -= 1
        count = 0 if running_tag(state) == "none" else 1
        respond(
            args,
            {
                "services": [
                    {
                        "desiredCount": count,
                        "runningCount": count,
                        "deployments": [
                            {
                                "status": "PRIMARY",
                                "rolloutState": state["rollout_state"],
                                "taskDefinition": task_definition_arn(logical_id, revision),
                            }
                        ],
                    }
                ]
            },
        )
    else:
        unsupported(state, "ecs", command)


def unsupported(state: dict[str, Any], service: str, command: str) -> None:
    print(f"stub: {service} {command} is not modelled", file=sys.stderr)
    sys.exit(2)


def curl(state: dict[str, Any], args: list[str]) -> None:
    body = {
        "status": "ready" if state["ready_code"] == 200 else "not_ready",
        "version": state["ready_version"] or running_tag(state),
        "checks": {"database": "ok", "schema": "ok", "secrets": "ok"},
    }
    target = option(args, "-o")
    if target:
        Path(target).write_text(json.dumps(body), encoding="utf-8")
    sys.stdout.write(str(state["ready_code"]))


def main() -> None:
    sys.stdout.reconfigure(newline="\n")  # type: ignore[union-attr]
    sys.stderr.reconfigure(newline="\n")  # type: ignore[union-attr]
    tool, args = sys.argv[1], sys.argv[2:]
    state_path = Path(os.environ["STUB_STATE"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["calls"].append([tool, *args])
    try:
        if tool == "curl":
            curl(state, args)
        elif args[0] == "cloudformation":
            cloudformation(state, args[1], args[2:])
        elif args[0] == "ecs":
            ecs(state, args[1], args[2:])
        else:
            unsupported(state, args[0], args[1] if len(args) > 1 else "")
    finally:
        state_path.write_text(json.dumps(state), encoding="utf-8")


if __name__ == "__main__":
    main()
