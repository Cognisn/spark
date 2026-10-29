"""Tests for the Origin and CSRF request guard (spark.web.security)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

import spark.web.security as security
from spark.web.security import CSRF_HEADER, OriginPolicy, RequestGuardMiddleware

TOKEN = "synthetic-csrf-token-0123456789abcdef"
ORIGIN = "http://127.0.0.1:22400"
BASE = ORIGIN


def _headers(
    origin: str | None = ORIGIN, token: str | None = TOKEN, **extra: str
) -> dict[str, str]:
    headers: dict[str, str] = dict(extra)
    if origin is not None:
        headers["Origin"] = origin
    if token is not None:
        headers[CSRF_HEADER] = token
    return headers


class TestOriginPolicyForInstance:
    policy = OriginPolicy.for_instance("http", "127.0.0.1", 22400)

    @pytest.mark.parametrize(
        "origin",
        ["http://127.0.0.1:22400", "http://localhost:22400", "http://[::1]:22400"],
    )
    def test_loopback_aliases_are_allowed(self, origin: str) -> None:
        assert self.policy.allows_origin(origin, host=origin.split("//")[1], scheme="http")

    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:22401",
            "https://127.0.0.1:22400",
            "http://attacker.example.com:22400",
            "http://example.com",
            "null",
            "",
        ],
    )
    def test_other_origins_are_refused(self, origin: str) -> None:
        assert not self.policy.allows_origin(origin, host="127.0.0.1:22400", scheme="http")

    def test_case_of_scheme_and_host_is_ignored(self) -> None:
        assert self.policy.allows_origin(
            "HTTP://LocalHost:22400", host="localhost:22400", scheme="http"
        )

    def test_default_port_is_omitted(self) -> None:
        policy = OriginPolicy.for_instance("https", "spark.example.com", 443)
        assert policy.allows_origin(
            "https://spark.example.com", host="spark.example.com", scheme="https"
        )

    def test_named_host_does_not_admit_loopback(self) -> None:
        policy = OriginPolicy.for_instance("http", "spark.example.com", 8080)
        assert not policy.allows_origin(
            "http://127.0.0.1:8080", host="127.0.0.1:8080", scheme="http"
        )

    def test_extra_origins_are_allowed(self) -> None:
        policy = OriginPolicy.for_instance(
            "http", "127.0.0.1", 22400, extra=["https://spark.example.org/"]
        )
        assert policy.allows_origin(
            "https://spark.example.org", host="spark.example.org", scheme="https"
        )

    def test_tls_terminating_proxy_with_an_extra_origin(self) -> None:
        # The proxy speaks plain HTTP to Spark and passes the public Host on.
        policy = OriginPolicy.for_instance(
            "http", "127.0.0.1", 22400, extra=["https://spark.example.org"]
        )
        assert policy.allows_host("spark.example.org")
        assert policy.allows_origin(
            "https://spark.example.org", host="spark.example.org", scheme="http"
        )
        assert not policy.allows_origin(
            "http://spark.example.org", host="spark.example.org", scheme="http"
        )

    def test_foreign_host_header_is_refused(self) -> None:
        # A page served under another name that resolves here (DNS rebinding)
        # is same-origin with itself, but its Host header is not ours.
        assert not self.policy.allows_origin(
            "http://rebind.example.net:22400", host="rebind.example.net:22400", scheme="http"
        )
        assert not self.policy.allows_host("rebind.example.net:22400")
        assert self.policy.allows_host("localhost:22400")

    @pytest.mark.parametrize("host", ["0.0.0.0", "::", ""])
    def test_wildcard_bind_falls_back_to_same_host(self, host: str) -> None:
        policy = OriginPolicy.for_instance("http", host, 22400)
        assert policy.allows_origin("http://192.0.2.5:22400", host="192.0.2.5:22400", scheme="http")
        assert not policy.allows_origin(
            "http://198.51.100.9:22400", host="192.0.2.5:22400", scheme="http"
        )


class TestSameHostPolicy:
    policy = OriginPolicy.same_host()

    def test_origin_must_match_the_host_header(self) -> None:
        assert self.policy.allows_origin("http://testserver", host="testserver", scheme="http")
        assert not self.policy.allows_origin("http://example.com", host="testserver", scheme="http")
        assert not self.policy.allows_origin("https://testserver", host="testserver", scheme="http")


def _guarded_app(
    expected_token: str | None = TOKEN,
    policy: OriginPolicy | None = None,
) -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    reached: list[str] = []
    active = policy or OriginPolicy.for_instance("http", "127.0.0.1", 22400)

    @app.get("/page")
    async def page() -> PlainTextResponse:
        reached.append("page")
        return PlainTextResponse("ok")

    @app.get("/stream/chat")
    async def stream() -> PlainTextResponse:
        reached.append("stream")
        return PlainTextResponse("data: ok\n\n", media_type="text/event-stream")

    @app.get("/events")
    async def events() -> PlainTextResponse:
        reached.append("events")
        return PlainTextResponse("data: ok\n\n", media_type="text/event-stream")

    @app.api_route("/change", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def change(request: Request) -> JSONResponse:
        reached.append(request.method)
        form = await request.form() if "form" in request.headers.get("content-type", "") else {}
        return JSONResponse({"code": str(form.get("code", ""))})

    def token_for(conn: HTTPConnection) -> str | None:
        return expected_token

    app.add_middleware(
        RequestGuardMiddleware,
        policy=lambda conn: active,
        csrf_token=token_for,
        stream_prefixes=("/stream/",),
    )
    return app, reached


@pytest.fixture()
def guarded() -> Iterator[tuple[TestClient, list[str]]]:
    app, reached = _guarded_app()
    with TestClient(app, base_url=BASE) as client:
        yield client, reached


class TestSafeRequests:
    def test_plain_get_needs_no_origin_or_token(
        self, guarded: tuple[TestClient, list[str]]
    ) -> None:
        client, reached = guarded
        assert client.get("/page").status_code == 200
        assert reached == ["page"]


class TestStateChangingRequests:
    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_good_origin_and_token_pass(
        self, guarded: tuple[TestClient, list[str]], method: str
    ) -> None:
        client, reached = guarded
        assert client.request(method, "/change", headers=_headers()).status_code == 200
        assert reached == [method]

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_missing_token_is_refused(
        self, guarded: tuple[TestClient, list[str]], method: str
    ) -> None:
        client, reached = guarded
        resp = client.request(method, "/change", headers=_headers(token=None))
        assert resp.status_code == 403
        assert "CSRF" in resp.json()["detail"]
        assert reached == []

    def test_wrong_token_is_refused(self, guarded: tuple[TestClient, list[str]]) -> None:
        client, reached = guarded
        resp = client.post("/change", headers=_headers(token=TOKEN[:-1] + "X"))
        assert resp.status_code == 403
        assert reached == []

    def test_non_ascii_token_is_refused_not_raised(
        self, guarded: tuple[TestClient, list[str]]
    ) -> None:
        client, reached = guarded
        resp = client.post(
            "/change", headers={"Origin": ORIGIN, CSRF_HEADER: "tökén".encode("utf-8")}
        )
        assert resp.status_code == 403

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    def test_cross_site_origin_is_refused(
        self, guarded: tuple[TestClient, list[str]], method: str
    ) -> None:
        client, reached = guarded
        resp = client.request(method, "/change", headers=_headers(origin="http://example.com"))
        assert resp.status_code == 403
        assert "Origin" in resp.json()["detail"]
        assert reached == []

    def test_no_origin_referer_or_fetch_metadata_is_refused(
        self, guarded: tuple[TestClient, list[str]]
    ) -> None:
        client, reached = guarded
        assert client.post("/change", headers=_headers(origin=None)).status_code == 403
        assert reached == []

    def test_same_origin_referer_stands_in_for_origin(
        self, guarded: tuple[TestClient, list[str]]
    ) -> None:
        client, reached = guarded
        headers = _headers(origin=None, Referer=f"{ORIGIN}/chat/1?x=y")
        assert client.post("/change", headers=headers).status_code == 200

    def test_cross_site_referer_is_refused(self, guarded: tuple[TestClient, list[str]]) -> None:
        client, reached = guarded
        headers = _headers(origin=None, Referer="http://example.com/page")
        assert client.post("/change", headers=headers).status_code == 403

    def test_fetch_metadata_same_origin_stands_in(
        self, guarded: tuple[TestClient, list[str]]
    ) -> None:
        client, reached = guarded
        headers = _headers(origin=None, **{"Sec-Fetch-Site": "same-origin"})
        assert client.post("/change", headers=headers).status_code == 200

    def test_fetch_metadata_cross_site_is_refused(
        self, guarded: tuple[TestClient, list[str]]
    ) -> None:
        client, reached = guarded
        headers = _headers(origin=None, **{"Sec-Fetch-Site": "cross-site"})
        assert client.post("/change", headers=headers).status_code == 403

    def test_foreign_host_header_is_refused(self, guarded: tuple[TestClient, list[str]]) -> None:
        client, reached = guarded
        headers = _headers(
            origin="http://rebind.example.net:22400", Host="rebind.example.net:22400"
        )
        assert client.post("/change", headers=headers).status_code == 403
        headers = _headers(origin=None, Host="rebind.example.net:22400")
        headers["Sec-Fetch-Site"] = "same-origin"
        assert client.post("/change", headers=headers).status_code == 403
        assert reached == []

    def test_form_field_token_is_accepted_and_the_body_still_reaches_the_endpoint(
        self, guarded: tuple[TestClient, list[str]]
    ) -> None:
        client, reached = guarded
        resp = client.post(
            "/change",
            headers=_headers(token=None),
            data={"csrf_token": TOKEN, "code": "ABCDEF01"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"code": "ABCDEF01"}

    def test_wrong_form_field_token_is_refused(self, guarded: tuple[TestClient, list[str]]) -> None:
        client, reached = guarded
        resp = client.post(
            "/change", headers=_headers(token=None), data={"csrf_token": "wrong", "code": "A"}
        )
        assert resp.status_code == 403
        assert reached == []

    def test_multipart_needs_the_header(self, guarded: tuple[TestClient, list[str]]) -> None:
        client, reached = guarded
        resp = client.post(
            "/change",
            headers=_headers(token=None),
            data={"csrf_token": TOKEN},
            files={"upload": ("a.txt", b"synthetic")},
        )
        assert resp.status_code == 403

    def test_no_expected_token_refuses_everything(self) -> None:
        app, reached = _guarded_app(expected_token=None)
        with TestClient(app, base_url=BASE) as client:
            assert client.post("/change", headers=_headers(token="")).status_code == 403
            assert client.post("/change", headers=_headers()).status_code == 403
        assert reached == []

    def test_html_clients_get_a_readable_page(self, guarded: tuple[TestClient, list[str]]) -> None:
        client, _ = guarded
        resp = client.post("/change", headers=_headers(token=None, Accept="text/html"))
        assert resp.status_code == 403
        assert resp.headers["content-type"].startswith("text/html")
        assert "CSRF" in resp.text


class TestEventStreams:
    def test_stream_with_our_referer_passes(self, guarded: tuple[TestClient, list[str]]) -> None:
        client, reached = guarded
        resp = client.get("/stream/chat", headers={"Referer": f"{ORIGIN}/chat/1"})
        assert resp.status_code == 200
        assert reached == ["stream"]

    def test_stream_from_another_origin_is_refused(
        self, guarded: tuple[TestClient, list[str]]
    ) -> None:
        client, reached = guarded
        resp = client.get("/stream/chat", headers={"Origin": "http://example.com"})
        assert resp.status_code == 403
        assert reached == []

    def test_stream_without_any_provenance_is_refused(
        self, guarded: tuple[TestClient, list[str]]
    ) -> None:
        client, reached = guarded
        assert client.get("/stream/chat").status_code == 403
        assert reached == []

    def test_event_stream_accept_header_marks_a_stream_anywhere(
        self, guarded: tuple[TestClient, list[str]]
    ) -> None:
        client, reached = guarded
        resp = client.get(
            "/events", headers={"Accept": "text/event-stream", "Origin": "http://example.com"}
        )
        assert resp.status_code == 403
        assert reached == []

    def test_stream_needs_no_csrf_token(self, guarded: tuple[TestClient, list[str]]) -> None:
        client, reached = guarded
        resp = client.get("/stream/chat", headers={"Sec-Fetch-Site": "same-origin"})
        assert resp.status_code == 200


class TestConstantTime:
    def test_token_is_compared_with_compare_digest(
        self, guarded: tuple[TestClient, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, _ = guarded
        calls: list[tuple[object, object]] = []
        real = security.hmac.compare_digest

        def spy(a: bytes, b: bytes) -> bool:
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(security.hmac, "compare_digest", spy)
        assert client.post("/change", headers=_headers(token="wrong")).status_code == 403
        assert client.post("/change", headers=_headers()).status_code == 200
        assert len(calls) == 2
