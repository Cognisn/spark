"""Static checks of deploy/compose.yaml, the example env file and .dockerignore."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "deploy" / "compose.yaml"
EXAMPLE = ROOT / "deploy" / "spark.env.example"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return data


def _services(compose: dict[str, Any]) -> dict[str, Any]:
    services: dict[str, Any] = compose["services"]
    return services


SPARK_SERVICES = ("migrate", "web", "worker")


def test_the_services(compose: dict[str, Any]) -> None:
    assert set(_services(compose)) == {"postgres", "mailpit", "migrate", "web", "worker"}
    assert _services(compose)["postgres"]["image"] == "pgvector/pgvector:pg16"
    assert "postgres-data:/var/lib/postgresql/data" in _services(compose)["postgres"]["volumes"]


def test_every_published_port_binds_to_loopback_by_default(compose: dict[str, Any]) -> None:
    published = []
    for name, service in _services(compose).items():
        for port in service.get("ports", []):
            published.append((name, port))
            assert str(port).startswith("${SPARK_BIND:-127.0.0.1}:"), (name, port)
    assert published, "the web role must be published"
    # Spark's containers share the mail catcher's network namespace, so it publishes
    # the web port (8000) beside its own inbox (8025).
    assert [name for name, _ in published] == ["mailpit", "mailpit"]
    assert [str(port).rsplit(":", 1)[1] for _, port in published] == ["8000", "8025"]


def test_a_local_mail_catcher_on_loopback(compose: dict[str, Any]) -> None:
    services = _services(compose)
    assert services["mailpit"]["image"].startswith("axllent/mailpit:v")
    for name in SPARK_SERVICES:
        # Spark sends plain-text SMTP only to a loopback host: the catcher's port is
        # on the shared namespace's loopback.
        assert services[name]["network_mode"] == "service:mailpit", name
        assert "ports" not in services[name], name
    values = _example_values()
    assert values["SPARK__MAIL__SMTP__HOST"] == "127.0.0.1"
    assert values["SPARK__MAIL__SMTP__PORT"] == "1025"
    assert values["SPARK__MAIL__SMTP__TLS"] == "none"


def test_migrate_and_bootstrap_run_before_web_and_worker(compose: dict[str, Any]) -> None:
    services = _services(compose)
    assert services["migrate"]["entrypoint"] == ["/bin/sh", "-c"]
    assert services["migrate"]["command"] == ["spark migrate && spark bootstrap"]
    assert services["migrate"]["restart"] == "no"
    for role in ("web", "worker"):
        assert services[role]["command"] == [role]
        # Only migrate: bootstrap has created the identity, so the worker no longer
        # waits for web.
        assert services[role]["depends_on"] == {
            "migrate": {"condition": "service_completed_successfully"}
        }


def test_spark_services_run_in_server_mode_from_one_image(compose: dict[str, Any]) -> None:
    services = _services(compose)
    images = {services[name]["image"] for name in ("migrate", "web", "worker")}
    assert len(images) == 1
    for name in ("migrate", "web", "worker"):
        assert services[name]["environment"]["SPARK__MODE"] == "server"


def test_no_credentials_in_the_compose_file() -> None:
    text = COMPOSE.read_text(encoding="utf-8")
    assert not re.search(r"(?i)(password|master_key|secret)\s*[:=]\s*\S", text)
    assert "postgresql://" not in text


def test_credentials_come_from_a_required_env_file(compose: dict[str, Any]) -> None:
    for name, service in _services(compose).items():
        if name == "mailpit":
            assert "env_file" not in service  # the catcher is given no credential
            continue
        env_files = service["env_file"]
        assert env_files == [{"path": "${SPARK_ENV_FILE:-spark.env}", "required": True}], name


def _example_values() -> dict[str, str]:
    return dict(
        line.split("=", 1)  # type: ignore[misc]
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def test_the_example_env_file_holds_only_dev_only_credentials() -> None:
    values = _example_values()
    for key in ("POSTGRES_PASSWORD", "KONFIG_MASTER_KEY"):
        assert values[key].endswith("-dev-only"), key
    assert "spark-db-password-dev-only@" in values["SPARK__DATABASE__URL"]


def test_the_example_names_a_placeholder_first_administrator() -> None:
    values = _example_values()
    assert values["SPARK__BOOTSTRAP__ADMIN__EMAIL"] == "CHANGEME@example.com"
    assert values["SPARK__BOOTSTRAP__ADMIN__FIRST_NAME"] == "CHANGEME"
    assert values["SPARK__BOOTSTRAP__ADMIN__LAST_NAME"] == "CHANGEME"
    text = EXAMPLE.read_text(encoding="utf-8")
    for login in ("spark_web:CHANGEME", "spark_worker:CHANGEME", "spark_purger:CHANGEME"):
        assert login in text


def test_dockerignore_admits_only_what_the_image_needs() -> None:
    lines = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines[0] == "*"
    admitted = {line[1:] for line in lines if line.startswith("!")}
    assert admitted == {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "src/",
        "scripts/licence_audit.py",
        "scripts/check_no_nvidia.py",
    }


FILES_ROOT = "/var/lib/spark-files"
SECRETS_FILE = "/var/lib/spark/data/spark/secrets.enc"
DOCKERFILE = ROOT / "Dockerfile"


def test_file_areas_live_on_a_named_volume_shared_by_web_and_worker(
    compose: dict[str, Any],
) -> None:
    services = _services(compose)
    assert "spark-files" in compose["volumes"]
    for role in ("web", "worker"):
        assert services[role]["environment"]["SPARK__FILES__ROOT"] == FILES_ROOT, role
        assert f"spark-files:{FILES_ROOT}" in services[role]["volumes"], role


def test_the_shared_secrets_file_is_named_on_the_data_volume(compose: dict[str, Any]) -> None:
    services = _services(compose)
    for role in ("migrate", "web", "worker"):
        env = services[role]["environment"]
        assert env["SPARK__SECRETS__BACKEND"] == "encrypted_file", role
        assert env["SPARK__SECRETS__FILE_PATH"] == SECRETS_FILE, role
        assert "spark-data:/var/lib/spark" in services[role]["volumes"], role
    assert SECRETS_FILE.startswith("/var/lib/spark/")


def test_image_installs_cpu_only_torch_before_spark_and_refuses_nvidia() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    torch = text.index('pip install torch --index-url "${TORCH_INDEX_URL}"')
    spark = text.index("RUN pip install .")
    check = text.index("RUN python scripts/check_no_nvidia.py")
    assert torch < spark < check
    assert "ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu" in text
    assert "COPY scripts/check_no_nvidia.py ./scripts/check_no_nvidia.py" in text


def test_image_prepares_the_files_volume_for_the_unprivileged_user() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert FILES_ROOT in text.split("chown -R 10001:10001", 1)[1].splitlines()[0]
    assert f'"{FILES_ROOT}"' in text.split("VOLUME", 1)[1].splitlines()[0]


def test_container_docs_name_the_pgvector_minimum_and_the_volumes() -> None:
    text = (ROOT / "docs" / "container.md").read_text(encoding="utf-8")
    assert "pgvector 0.8 or later" in text
    assert "spark-files" in text and "SPARK__FILES__ROOT" in text
    assert "SPARK__SECRETS__FILE_PATH" in text
