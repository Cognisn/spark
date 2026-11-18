"""The provider setup guides and help text point at connections, not 0.2 providers (Q51)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spark.web.endpoints import help as help_endpoint
from spark.web.endpoints import provider_guides

TEMPLATE = (
    Path(provider_guides.__file__).resolve().parents[2] / "resources" / "config.yaml.template"
)
RETIRED = ("LLM Providers", "section-providers", "Save Settings", "Enable at least one provider")


def _client(identity_attached: bool | None) -> TestClient:
    app = FastAPI()
    app.include_router(provider_guides.router)
    if identity_attached is not None:
        app.state.identity_attached = identity_attached
    return TestClient(app)


@pytest.mark.parametrize(
    ("identity_attached", "target"),
    [
        (None, "/settings#section-connections"),
        (False, "/settings#section-connections"),
        (True, "/admin/connections"),
    ],
)
def test_an_unknown_guide_redirects_to_the_connections_screen(
    identity_attached: bool | None, target: str
) -> None:
    resp = _client(identity_attached).get("/help/provider/no-such-guide", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == target


def test_the_guides_describe_connections() -> None:
    text = repr(provider_guides.PROVIDER_GUIDES)
    for phrase in RETIRED:
        assert phrase not in text
    for guide in provider_guides.PROVIDER_GUIDES.values():
        assert "Add connection" in repr(guide["spark_config"])


def test_the_help_topics_describe_connections() -> None:
    text = repr(help_endpoint.HELP_TOPICS)
    for phrase in RETIRED:
        assert phrase not in text
    titles = {t["id"]: t["title"] for t in help_endpoint.HELP_TOPICS}
    assert titles["providers"] == "Connections"


def test_the_config_template_names_no_provider_secret() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "secret://providers_" not in text
    assert "Administrators change providers" not in text
    assert "No code reads this section" in text
