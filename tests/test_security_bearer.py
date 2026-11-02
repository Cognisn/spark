"""The request guard's exemption for requests authenticated by a bearer API key.

A bearer request carries no ambient credential, so it needs no CSRF token; a
browser request (one with Origin, Referer or fetch metadata) is still held to
the instance's origins. Without the exemption every guard stays as it was.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from spark.web.security import (
    CSRF_HEADER,
    CSRF_REFUSED,
    ORIGIN_REFUSED,
    OriginPolicy,
    RequestGuardMiddleware,
)

ORIGIN = "https://spark.example.com"


def _app(exempt: bool | None) -> FastAPI:
    app = FastAPI()

    @app.post("/act")
    def act() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/stream/x")
    def stream() -> dict[str, str]:
        return {"ok": "stream"}

    def is_exempt(conn: HTTPConnection) -> bool:
        return bool(exempt)

    options: dict[str, Any] = {} if exempt is None else {"csrf_exempt": is_exempt}
    app.add_middleware(
        RequestGuardMiddleware,
        policy=lambda conn: OriginPolicy(frozenset({ORIGIN})),
        csrf_token=lambda conn: "the-token",
        **options,
    )
    return app


def _client(exempt: bool | None) -> TestClient:
    return TestClient(_app(exempt), base_url=ORIGIN)


class TestExempt:
    def test_no_csrf_token_and_no_provenance_needed(self) -> None:
        assert _client(True).post("/act").status_code == 200

    def test_a_browser_origin_is_still_checked(self) -> None:
        resp = _client(True).post("/act", headers={"Origin": "https://example.org"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == ORIGIN_REFUSED

    def test_a_referer_is_still_checked(self) -> None:
        resp = _client(True).post("/act", headers={"Referer": "https://example.org/page"})
        assert resp.status_code == 403

    def test_cross_site_fetch_metadata_is_refused(self) -> None:
        resp = _client(True).post("/act", headers={"Sec-Fetch-Site": "cross-site"})
        assert resp.status_code == 403

    def test_the_own_origin_passes(self) -> None:
        assert _client(True).post("/act", headers={"Origin": ORIGIN}).status_code == 200

    def test_a_stream_from_another_origin_is_refused(self) -> None:
        resp = _client(True).get("/stream/x", headers={"Origin": "https://example.org"})
        assert resp.status_code == 403

    def test_a_spoofed_host_is_refused(self) -> None:
        resp = _client(True).post("/act", headers={"Host": "evil.example.net"})
        assert resp.status_code == 403


@pytest.mark.parametrize("exempt", [None, False])
class TestNotExempt:
    def test_csrf_still_required(self, exempt: bool | None) -> None:
        resp = _client(exempt).post("/act", headers={"Origin": ORIGIN})
        assert resp.status_code == 403
        assert resp.json()["detail"] == CSRF_REFUSED

    def test_provenance_still_required(self, exempt: bool | None) -> None:
        resp = _client(exempt).post("/act", headers={CSRF_HEADER: "the-token"})
        assert resp.status_code == 403

    def test_both_present_pass(self, exempt: bool | None) -> None:
        resp = _client(exempt).post("/act", headers={"Origin": ORIGIN, CSRF_HEADER: "the-token"})
        assert resp.status_code == 200


class TestPinnedPolicy:
    @pytest.mark.parametrize(
        ("base", "origin"),
        [
            ("https://spark.example.com", "https://spark.example.com"),
            ("https://spark.example.com/", "https://spark.example.com"),
            ("https://Spark.Example.com:443", "https://spark.example.com"),
            ("https://spark.example.com:8443", "https://spark.example.com:8443"),
            ("http://127.0.0.1:22401", "http://127.0.0.1:22401"),
        ],
    )
    def test_pinned_to_one_origin(self, base: str, origin: str) -> None:
        policy = OriginPolicy.pinned(base, ["https://other.example.com/"])
        assert policy == OriginPolicy(frozenset({origin, "https://other.example.com"}))
        assert not policy.match_host

    def test_request_state_is_visible_to_the_exemption(self) -> None:
        # The auth layer runs first and marks the request in its state.
        app = FastAPI()

        @app.post("/act")
        def act(request: Request) -> dict[str, bool]:
            return {"bearer": bool(getattr(request.state, "bearer", False))}

        app.add_middleware(
            RequestGuardMiddleware,
            policy=lambda conn: OriginPolicy(frozenset({ORIGIN})),
            csrf_token=lambda conn: None,
            csrf_exempt=lambda conn: bool(getattr(conn.state, "bearer", False)),
        )

        @app.middleware("http")
        async def mark(request: Request, call_next):  # type: ignore[no-untyped-def]
            request.state.bearer = request.headers.get("authorization") == "Bearer k"
            return await call_next(request)

        client = TestClient(app, base_url=ORIGIN)
        assert client.post("/act", headers={"Authorization": "Bearer k"}).json() == {"bearer": True}
        assert client.post("/act", headers={"Authorization": "Bearer x"}).status_code == 403
