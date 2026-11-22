"""Static checks that every Spark container in the deploy files stays hardened.

Every service that runs the Spark image, in ``deploy/compose.yaml``, the Portainer
and Docker Desktop stacks, and the live end-to-end overlay (merged over
``deploy/compose.yaml`` as ``deploy/live/stack.py`` runs it), must keep a read-only
root filesystem, drop every Linux capability, refuse privilege gain, run an init
process, use Docker's default seccomp profile and mount ``/tmp`` as a size-limited
tmpfs that allows no programs to run from it. See
docs/deployment/container-hardening.md.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
COMPOSE = DEPLOY / "compose.yaml"
PORTAINER = DEPLOY / "portainer" / "stack.yaml"
DESKTOP = DEPLOY / "docker-desktop" / "compose.yaml"
LIVE = DEPLOY / "live" / "compose.live.yaml"
DOCKERFILE = ROOT / "Dockerfile"
GUIDE = ROOT / "docs" / "deployment" / "container-hardening.md"

#: The Spark services each file must hold, so a renamed or added one is not missed.
EXPECTED = {
    "compose": {"migrate", "web", "worker"},
    "portainer": {"migrate", "web", "worker"},
    "desktop": {"migrate", "web", "worker"},
    "live": {"migrate", "web", "web2", "worker", "mcp"},
}
#: The volumes every Spark role keeps (the only places Spark writes besides /tmp).
VOLUMES = ("/var/lib/spark", "/var/lib/spark-files", "/var/cache/spark/huggingface")
#: Security options that would undo a control.
UNSAFE_OPTIONS = (
    "seccomp=unconfined",
    "seccomp:unconfined",
    "apparmor=unconfined",
    "apparmor:unconfined",
    "label=disable",
    "label:disable",
    "systempaths=unconfined",
)


def _load(path: Path) -> dict[str, Any]:
    # Compose's merge tags (``!reset``, ``!override``) only say how an overlay merges;
    # the value after them is what this test reads.
    text = re.sub(r"!(reset|override)\b", "", path.read_text(encoding="utf-8"))
    data: dict[str, Any] = yaml.safe_load(text)
    return data


def _services(path: Path) -> dict[str, dict[str, Any]]:
    services: dict[str, dict[str, Any]] = _load(path)["services"]
    return services


def _live_services() -> dict[str, dict[str, Any]]:
    """The overlay over deploy/compose.yaml, merged service by service as Compose would
    for the keys this test reads: the overlay's volumes are added to the base's, and any
    other key the overlay sets replaces the base's (the stricter reading for a test)."""
    merged = {name: dict(service) for name, service in _services(COMPOSE).items()}
    for name, service in _services(LIVE).items():
        target = merged.setdefault(name, {})
        for key, value in service.items():
            if key == "volumes":
                target[key] = list(target.get(key, [])) + list(value)
            else:
                target[key] = value
    return merged


def _stack(name: str) -> dict[str, dict[str, Any]]:
    if name == "live":
        return _live_services()
    return _services({"compose": COMPOSE, "portainer": PORTAINER, "desktop": DESKTOP}[name])


def _is_spark(service: dict[str, Any]) -> bool:
    image = str(service.get("image", ""))
    return image.startswith("${SPARK_IMAGE") or image.startswith("ghcr.io/cognisn/spark")


def _spark_services(name: str) -> dict[str, dict[str, Any]]:
    return {n: s for n, s in _stack(name).items() if _is_spark(s)}


def _cases() -> list[tuple[str, str]]:
    return [(stack, service) for stack in sorted(EXPECTED) for service in sorted(EXPECTED[stack])]


@pytest.mark.parametrize("stack", sorted(EXPECTED))
def test_every_spark_service_is_known(stack: str) -> None:
    assert set(_spark_services(stack)) == EXPECTED[stack]


@pytest.mark.parametrize(("stack", "name"), _cases())
def test_the_root_filesystem_is_read_only(stack: str, name: str) -> None:
    assert _spark_services(stack)[name].get("read_only") is True, (stack, name)


@pytest.mark.parametrize(("stack", "name"), _cases())
def test_every_capability_is_dropped_and_none_added(stack: str, name: str) -> None:
    service = _spark_services(stack)[name]
    assert [str(c).upper() for c in service.get("cap_drop", [])] == ["ALL"], (stack, name)
    assert not service.get("cap_add"), (stack, name)
    assert not service.get("privileged"), (stack, name)
    assert not service.get("devices"), (stack, name)


@pytest.mark.parametrize(("stack", "name"), _cases())
def test_no_new_privileges_and_the_default_seccomp_profile(stack: str, name: str) -> None:
    options = [
        str(o).replace(" ", "") for o in _spark_services(stack)[name].get("security_opt", [])
    ]
    assert "no-new-privileges:true" in options, (stack, name, options)
    for option in options:
        # Docker's default seccomp profile applies only while no seccomp option is named.
        assert not option.startswith("seccomp"), (stack, name, option)
        assert option not in UNSAFE_OPTIONS, (stack, name, option)


@pytest.mark.parametrize(("stack", "name"), _cases())
def test_an_init_process_and_no_host_namespaces(stack: str, name: str) -> None:
    service = _spark_services(stack)[name]
    assert service.get("init") is True, (stack, name)
    for key in ("pid", "ipc", "userns_mode", "uts"):
        assert service.get(key) != "host", (stack, name, key)
    assert service.get("network_mode") != "host", (stack, name)
    assert service.get("user") in (None, "10001:10001", "10001"), (stack, name)


@pytest.mark.parametrize(("stack", "name"), _cases())
def test_tmp_is_a_size_limited_tmpfs_that_runs_no_programs(stack: str, name: str) -> None:
    entries = _spark_services(stack)[name].get("tmpfs", [])
    if isinstance(entries, str):
        entries = [entries]
    mounts = {str(e).split(":", 1)[0]: str(e).partition(":")[2] for e in entries}
    assert "/tmp" in mounts, (stack, name, entries)
    for path, raw in mounts.items():
        options = set(raw.split(","))
        assert {"noexec", "nosuid", "nodev"} <= options, (stack, name, path, raw)
        assert any(re.fullmatch(r"size=\d+[kmg]?", o) for o in options), (stack, name, path, raw)


@pytest.mark.parametrize(("stack", "name"), [c for c in _cases() if c[1] != "mcp"])
def test_the_spark_volumes_are_kept(stack: str, name: str) -> None:
    targets = {str(v).split(":")[1] for v in _spark_services(stack)[name].get("volumes", [])}
    assert set(VOLUMES) <= targets, (stack, name, targets)


def test_the_web_role_has_room_in_tmp_for_a_bundle_import() -> None:
    """Uploads spool to /tmp; a bundle import may be 1 GiB (spark.bundles.reader)."""
    for stack in ("compose", "portainer", "desktop", "live"):
        for name in ("web", "web2"):
            service = _spark_services(stack).get(name)
            if service is None:
                continue
            (entry,) = [e for e in service["tmpfs"] if str(e).startswith("/tmp:")]
            size = re.search(r"size=(\d+)([kmg]?)", entry)
            assert size, entry
            scale = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}[size.group(2)]
            assert int(size.group(1)) * scale >= 2 << 30, (stack, name, entry)


def test_the_image_writes_only_to_its_volumes_and_tmp() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^USER 10001:10001$", text, re.M)
    volume = re.search(r"^VOLUME \[(.*)\]$", text, re.M)
    assert volume, "the image declares its volumes"
    declared = {v.strip().strip('"') for v in volume.group(1).split(",")}
    # /tmp is declared so a platform that copies a volume's mode from the image
    # (ECS on Fargate) gives the unprivileged user a writable /tmp.
    assert declared == set(VOLUMES) | {"/tmp"}, declared
    for variable in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "HF_HOME"):
        value = re.search(rf"\b{variable}=(\S+)", text)
        assert value and value.group(1).startswith(VOLUMES), variable
    cache = re.search(r"\bTIKTOKEN_CACHE_DIR=(\S+)", text)
    assert cache and cache.group(1).startswith(VOLUMES), "tiktoken's cache is on a volume"


def test_the_guide_gives_the_ecs_equivalents() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    for setting in (
        '"readonlyRootFilesystem": true',
        '"user": "10001:10001"',
        '"drop": ["ALL"]',
        '"initProcessEnabled": true',
        '"enableExecuteCommand": false',
        '"containerPath": "/tmp"',
    ):
        assert setting in text, setting
