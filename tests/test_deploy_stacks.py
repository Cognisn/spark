"""Static checks of the Docker Desktop and Portainer stacks beside deploy/compose.yaml."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "compose.yaml"
DESKTOP = ROOT / "deploy" / "docker-desktop" / "compose.yaml"
DESKTOP_EXAMPLE = ROOT / "deploy" / "docker-desktop" / "spark.env.example"
PORTAINER = ROOT / "deploy" / "portainer" / "stack.yaml"
STACKS = {"desktop": DESKTOP, "portainer": PORTAINER}
SPARK_SERVICES = ("migrate", "web", "worker")
# Settings every Spark container must share with deploy/compose.yaml.
SHARED_SETTINGS = (
    "SPARK__MODE",
    "SPARK__SECRETS__BACKEND",
    "SPARK__SECRETS__FILE_PATH",
    "SPARK__FILES__ROOT",
)


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


@pytest.fixture(scope="module", params=sorted(STACKS))
def stack(request: pytest.FixtureRequest) -> tuple[str, dict[str, Any]]:
    name: str = request.param
    return name, _load(STACKS[name])


def _services(data: dict[str, Any]) -> dict[str, Any]:
    services: dict[str, Any] = data["services"]
    return services


def test_the_services() -> None:
    assert set(_services(_load(PORTAINER))) == {"postgres", "mailpit", "migrate", "web", "worker"}
    desktop = _services(_load(DESKTOP))
    assert set(desktop) == {"postgres", "mailpit", "migrate", "web", "worker", "https"}
    assert desktop["https"]["profiles"] == ["https"]


def test_every_published_port_binds_to_loopback_by_default(
    stack: tuple[str, dict[str, Any]],
) -> None:
    name, data = stack
    published = []
    for service_name, service in _services(data).items():
        for port in service.get("ports", []):
            published.append((service_name, port))
            assert re.match(r"^(127\.0\.0\.1|\$\{SPARK_BIND:-127\.0\.0\.1\}):", str(port)), (
                name,
                service_name,
                port,
            )
    assert published, name
    # Spark's containers share the mail catcher's namespace, so it publishes the web
    # port (8000) beside its own inbox (8025); no Spark service publishes anything.
    spark = [(n, port) for n, port in published if n != "https"]
    assert [n for n, _ in spark] == ["mailpit", "mailpit"], name
    assert [str(port).rsplit(":", 1)[1] for _, port in spark] == ["8000", "8025"], name
    inbox = spark[1][1]
    assert str(inbox).startswith("127.0.0.1:"), (name, inbox)


def test_spark_services_run_in_server_mode_with_the_compose_files_settings(
    stack: tuple[str, dict[str, Any]],
) -> None:
    name, data = stack
    reference = _services(_load(COMPOSE))["web"]
    services = _services(data)
    for service_name in SPARK_SERVICES:
        service = services[service_name]
        env = service["environment"]
        for key in SHARED_SETTINGS:
            assert env[key] == reference["environment"][key], (name, service_name, key)
        assert service["volumes"] == reference["volumes"], (name, service_name)
        assert service["image"] == services["web"]["image"], (name, service_name)
        assert "KONFIG_MASTER_KEY" in env and "SPARK__SERVER__PUBLIC_BASE_URL" in env


def test_the_image_is_pinned_never_latest(stack: tuple[str, dict[str, Any]]) -> None:
    name, data = stack
    image = _services(data)["web"]["image"]
    assert image.startswith("${SPARK_IMAGE"), (name, image)
    assert "latest" not in image, (name, image)
    assert re.search(r"ghcr\.io/cognisn/spark:\d+\.\d+\.\d+(rc\d+)?\b", image), (name, image)


def test_migrate_and_bootstrap_run_before_web_and_worker(
    stack: tuple[str, dict[str, Any]],
) -> None:
    name, data = stack
    services = _services(data)
    reference = _services(_load(COMPOSE))["migrate"]
    assert services["migrate"]["entrypoint"] == reference["entrypoint"] == ["/bin/sh", "-c"]
    assert services["migrate"]["command"] == reference["command"]
    assert services["migrate"]["command"] == ["spark migrate && spark bootstrap"]
    assert services["migrate"]["restart"] == "no"
    assert services["migrate"]["depends_on"] == {
        "postgres": {"condition": "service_healthy"},
        "mailpit": {"condition": "service_started"},
    }, name
    for role in ("web", "worker"):
        assert services[role]["command"] == [role], (name, role)
        # Only migrate: bootstrap has created the identity, so the worker no longer
        # waits for web (decision 112).
        assert services[role]["depends_on"] == {
            "migrate": {"condition": "service_completed_successfully"}
        }, (name, role)


def test_a_local_mail_catcher_on_loopback(stack: tuple[str, dict[str, Any]]) -> None:
    name, data = stack
    services = _services(data)
    reference = _services(_load(COMPOSE))
    assert services["mailpit"]["image"] == reference["mailpit"]["image"], name
    assert "env_file" not in services["mailpit"] and "environment" not in services["mailpit"]
    for role in SPARK_SERVICES:
        # Spark sends plain-text SMTP only to a loopback host (decision 113): the
        # catcher's port is on the shared namespace's loopback.
        assert services[role]["network_mode"] == "service:mailpit", (name, role)
        assert "ports" not in services[role], (name, role)
        env = services[role]["environment"]
        assert env["SPARK__MAIL__SMTP__HOST"] == "${SPARK__MAIL__SMTP__HOST:-127.0.0.1}"
        assert env["SPARK__MAIL__SMTP__PORT"] == "${SPARK__MAIL__SMTP__PORT:-1025}"
        assert env["SPARK__MAIL__SMTP__TLS"] == "${SPARK__MAIL__SMTP__TLS:-none}"
        assert env["SPARK__MAIL__SMTP__SENDER_ADDRESS"].startswith(
            "${SPARK__MAIL__SMTP__SENDER_ADDRESS:-"
        )


def test_the_first_administrator_comes_from_required_bootstrap_settings(
    stack: tuple[str, dict[str, Any]],
) -> None:
    name, data = stack
    for role in SPARK_SERVICES:
        env = _services(data)[role]["environment"]
        for setting in ("EMAIL", "FIRST_NAME", "LAST_NAME"):
            key = f"SPARK__BOOTSTRAP__ADMIN__{setting}"
            assert env[key].startswith("${" + key + ":?"), (name, role, key)


def test_credentials_are_required_variables_never_written_in(
    stack: tuple[str, dict[str, Any]],
) -> None:
    name, data = stack
    text = STACKS[name].read_text(encoding="utf-8")
    for variable in ("POSTGRES_PASSWORD", "KONFIG_MASTER_KEY"):
        assert re.search(r"\$\{" + variable + r":\?", text), (name, variable)
    assert "dev-only" not in text, name
    services = _services(data)
    assert "env_file" not in services["web"], name
    url = services["web"]["environment"]["SPARK__DATABASE__URL"]
    assert "${POSTGRES_PASSWORD:?" in url, (name, url)


def test_the_portainer_stack_requires_its_public_address_and_image() -> None:
    text = PORTAINER.read_text(encoding="utf-8")
    assert "${SPARK_PUBLIC_BASE_URL:?" in text
    assert "${SPARK_IMAGE:?" in text
    assert "name:" not in [line.split(" ")[0] for line in text.splitlines()]


def test_the_desktop_stack_defaults_to_a_loopback_address() -> None:
    env = _services(_load(DESKTOP))["web"]["environment"]
    assert (
        env["SPARK__SERVER__PUBLIC_BASE_URL"] == "${SPARK_PUBLIC_BASE_URL:-http://localhost:8000}"
    )
    https = _services(_load(DESKTOP))["https"]
    assert "https://localhost:8443" in https["command"]
    # Spark's containers share the mail catcher's network namespace.
    assert "mailpit:8000" in https["command"]
    assert "web:8000" not in https["command"]
    assert "--change-host-header" not in https["command"]


def test_the_desktop_example_env_file_holds_only_dev_only_credentials() -> None:
    values = {}
    for line in DESKTOP_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key] = value
    assert values["POSTGRES_PASSWORD"].endswith("-dev-only")
    assert values["KONFIG_MASTER_KEY"].endswith("-dev-only")
    # The placeholder first administrator, invited by spark bootstrap.
    assert values["SPARK__BOOTSTRAP__ADMIN__EMAIL"] == "CHANGEME@example.com"
    assert values["SPARK__BOOTSTRAP__ADMIN__FIRST_NAME"] == "CHANGEME"
    assert values["SPARK__BOOTSTRAP__ADMIN__LAST_NAME"] == "CHANGEME"
    assert values["SPARK_MAIL_PORT"] == "8025"
    assert values["SPARK_PUBLIC_BASE_URL"] == "http://localhost:" + values["SPARK_WEB_PORT"]


def test_env_files_beside_the_stacks_are_ignored_by_git() -> None:
    ignore = (ROOT / "deploy" / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "*.env" in ignore


def test_the_guides_name_their_stack_files() -> None:
    docs = ROOT / "docs" / "deployment"
    assert "deploy/docker-desktop/compose.yaml" in (docs / "docker-desktop.md").read_text("utf-8")
    assert "deploy/portainer/stack.yaml" in (docs / "portainer.md").read_text("utf-8")
    index = (docs / "index.md").read_text("utf-8")
    assert "(portainer.md)" in index and "(docker-desktop.md)" in index


def test_the_pages_describe_bootstrap_as_the_first_run() -> None:
    pages = [
        ROOT / "docs" / "deployment" / "docker-desktop.md",
        ROOT / "docs" / "deployment" / "portainer.md",
        ROOT / "docs" / "deployment" / "index.md",
        ROOT / "README.md",
    ]
    for page in pages:
        text = page.read_text("utf-8")
        assert "spark bootstrap" in text, page.name
        # create-admin appears only as recovery, never as the first-run step.
        if "create-admin" in text:
            assert "recover" in text.lower() or "restor" in text.lower(), page.name
