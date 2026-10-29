"""Every state-changing endpoint, router by router, refuses without the CSRF token.

The routes are read from the application itself, so a new endpoint is covered
as soon as it is registered. Requests are made by a signed-in client on the
instance's own origin; only the token (or the origin) is taken away, and the
guard must refuse before the endpoint runs.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from spark.web import endpoints
from spark.web.auth import LOGIN_PATH
from spark.web.security import CSRF_HEADER, CSRF_REFUSED, ORIGIN_REFUSED
from spark.web.server import create_app
from tests.web_helpers import sign_in

UNSAFE = ("POST", "PUT", "PATCH", "DELETE")

# Every router module that registers a state-changing endpoint today.
ROUTERS_WITH_STATE_CHANGES = {
    "actions",
    "auth",
    "chat",
    "conversations",
    "debate",
    "knowledge",
    "main_menu",
    "mcp_servers",
    "memories",
    "panel",
    "settings",
    "skills",
    "streaming",
    "voice",
}


def _ctx() -> MagicMock:
    values: dict[str, Any] = {
        "interface.session_timeout_minutes": 60,
        "interface.host": "127.0.0.1",
    }

    def get(key: str, default: object = None, *, cast: type | None = None) -> object:
        val = values.get(key, default)
        return cast(val) if cast is not None and val is not None else val

    ctx = MagicMock()
    ctx.settings.get = get
    return ctx


def _app() -> FastAPI:
    return create_app(_ctx(), first_run=False)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _routes() -> list[tuple[str, APIRoute]]:
    """Every route of every endpoint module's router, with the module's name."""
    found = []
    for info in pkgutil.iter_modules(endpoints.__path__):
        module = importlib.import_module(f"{endpoints.__name__}.{info.name}")
        router = getattr(module, "router", None)
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute):
                found.append((info.name, route))
    return found


def _state_changing() -> list[tuple[str, str, str]]:
    return [
        (name, method, route.path)
        for name, route in _routes()
        for method in sorted(route.methods & set(UNSAFE))
    ]


def _streams() -> list[str]:
    return [
        route.path
        for _, route in _routes()
        if "GET" in route.methods and route.path.startswith("/stream/")
    ]


STATE_CHANGING = _state_changing()
STREAMS = _streams()


def test_enumeration_matches_the_served_application() -> None:
    schema = _app().openapi()
    served = {
        (method.upper(), path)
        for path, operations in schema["paths"].items()
        for method in operations
        if method.upper() in UNSAFE
    }
    assert served == {(method, path) for _, method, path in STATE_CHANGING}


@pytest.fixture()
def signed_in() -> TestClient:
    client = TestClient(_app())
    sign_in(client)
    return client


def test_every_router_with_state_changes_is_enumerated() -> None:
    assert {router for router, _, _ in STATE_CHANGING} == ROUTERS_WITH_STATE_CHANGES
    assert len(STATE_CHANGING) > 50


def test_streams_are_enumerated() -> None:
    assert {"/stream/chat", "/stream/debate", "/stream/panel"} <= set(STREAMS)


@pytest.mark.parametrize(
    ("router", "method", "path"), STATE_CHANGING, ids=[f"{r}:{m}:{p}" for r, m, p in STATE_CHANGING]
)
def test_refused_without_the_csrf_token(
    signed_in: TestClient, router: str, method: str, path: str
) -> None:
    del signed_in.headers[CSRF_HEADER]
    resp = signed_in.request(method, _concrete(path), follow_redirects=False)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == CSRF_REFUSED


@pytest.mark.parametrize(
    ("router", "method", "path"), STATE_CHANGING, ids=[f"{r}:{m}:{p}" for r, m, p in STATE_CHANGING]
)
def test_refused_with_a_wrong_csrf_token(
    signed_in: TestClient, router: str, method: str, path: str
) -> None:
    signed_in.headers[CSRF_HEADER] = "not-the-session-token"
    resp = signed_in.request(method, _concrete(path), follow_redirects=False)
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize(
    ("router", "method", "path"), STATE_CHANGING, ids=[f"{r}:{m}:{p}" for r, m, p in STATE_CHANGING]
)
def test_refused_from_another_origin_even_with_the_token(
    signed_in: TestClient, router: str, method: str, path: str
) -> None:
    signed_in.headers["Origin"] = "http://example.com"
    resp = signed_in.request(method, _concrete(path), follow_redirects=False)
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == ORIGIN_REFUSED


@pytest.mark.parametrize("path", STREAMS)
def test_streams_refused_from_another_origin(signed_in: TestClient, path: str) -> None:
    signed_in.headers["Origin"] = "http://example.com"
    resp = signed_in.get(_concrete(path) + "?conversation_id=1&message=x")
    assert resp.status_code == 403
    assert resp.json()["detail"] == ORIGIN_REFUSED


@pytest.mark.parametrize("path", STREAMS)
def test_streams_refused_without_provenance(signed_in: TestClient, path: str) -> None:
    del signed_in.headers["Origin"]
    resp = signed_in.get(_concrete(path) + "?conversation_id=1&message=x")
    assert resp.status_code == 403


def test_a_guarded_endpoint_still_works_with_the_token(signed_in: TestClient) -> None:
    resp = signed_in.post("/api/heartbeat")
    assert resp.status_code == 200


def test_the_login_endpoint_is_among_those_checked() -> None:
    assert ("auth", "POST", LOGIN_PATH) in STATE_CHANGING
