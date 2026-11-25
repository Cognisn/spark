"""The fetch guard (spark.preview.guard): every rule, and every refusal.

A stub link server (tests/preview_helpers.py) stands in for KVASIR's document links
on 127.0.0.1. Each refusal is a test of its own and was mutation-checked: the test
fails when its rule is removed from the guard.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest

from spark.preview import guard
from spark.preview.guard import LinkRefused, McpOrigin, fetch, is_public_address, match_origin
from spark.preview.kinds import Kind
from tests.preview_helpers import (
    Fixture,
    LinkServer,
    docx_bytes,
    origin_for,
    pdf_bytes,
    png_bytes,
    zip_bytes,
)

MB = 1024 * 1024


@pytest.fixture
def links() -> Any:
    with LinkServer() as server:
        yield server


def _fetch(url: str, origins: list[McpOrigin], **kwargs: Any) -> guard.Fetched:
    kwargs.setdefault("max_bytes", 5 * MB)
    kwargs.setdefault("timeout", 5.0)
    return fetch(url, origins, **kwargs)


class TestAllowed:
    def test_a_link_from_the_mcp_servers_host_is_fetched(self, links: LinkServer) -> None:
        body = pdf_bytes()
        url = links.add(
            "/document-links/v1/tok",
            Fixture(body, "application/pdf", 'inline; filename="report.pdf"'),
        )
        got = _fetch(url, [origin_for(links)])
        assert got.content == body
        assert got.kind is Kind.PDF and got.media_type == "application/pdf"
        assert got.filename == "report.pdf"

    def test_the_utf8_file_name_is_taken(self, links: LinkServer) -> None:
        url = links.add(
            "/d",
            Fixture(
                docx_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "attachment; filename=\"notes.docx\"; filename*=UTF-8''r%C3%A9sum%C3%A9.docx",
            ),
        )
        got = _fetch(url, [origin_for(links)])
        assert got.filename == "résumé.docx" and got.kind is Kind.DOCX

    def test_a_redirect_on_the_same_host_is_followed(self, links: LinkServer) -> None:
        links.add("/final", Fixture(png_bytes(), "image/png"))
        url = links.add("/start", Fixture(status=302, headers={"Location": "/final"}))
        got = _fetch(url, [origin_for(links)])
        assert got.kind is Kind.IMAGE and got.url.endswith("/final")

    def test_a_generic_type_takes_the_names_type_when_the_content_agrees(
        self, links: LinkServer
    ) -> None:
        url = links.add("/x/report.pdf", Fixture(pdf_bytes(), "application/octet-stream"))
        assert _fetch(url, [origin_for(links)]).kind is Kind.PDF

    def test_a_generic_type_whose_content_disagrees_is_only_offered(
        self, links: LinkServer
    ) -> None:
        url = links.add("/x/fake.pdf", Fixture(b"<html>no</html>", "application/octet-stream"))
        got = _fetch(url, [origin_for(links)])
        assert got.kind is Kind.OTHER

    def test_an_unknown_type_is_offered_for_download(self, links: LinkServer) -> None:
        url = links.add("/a.zip", Fixture(zip_bytes(), "application/zip"))
        assert _fetch(url, [origin_for(links)]).kind is Kind.OTHER


class TestSchemeAndHost:
    """Rules 1 and 2."""

    @pytest.mark.parametrize(
        "url", ["ftp://127.0.0.1/x", "file:///etc/passwd", "javascript:alert(1)", "//x/y"]
    )
    def test_other_schemes_are_refused(self, url: str, links: LinkServer) -> None:
        with pytest.raises(LinkRefused, match="Only https"):
            _fetch(url, [origin_for(links)])

    def test_http_is_refused_when_the_server_uses_https(self) -> None:
        origin = McpOrigin("kvasir", "https", "kvasir.example.org", 443)
        with pytest.raises(LinkRefused, match="scheme or port"):
            match_origin("http://kvasir.example.org/d", [origin])

    def test_https_is_allowed_for_an_http_server_on_the_default_port(self) -> None:
        origin = McpOrigin("kvasir", "http", "kvasir.internal", 8080)
        assert match_origin("https://kvasir.internal/d", [origin]) is origin
        with pytest.raises(LinkRefused):
            match_origin("https://kvasir.internal:8443/d", [origin])

    def test_another_host_is_refused(self, links: LinkServer) -> None:
        links.add("/d", Fixture(b"hello", "text/plain"))
        with pytest.raises(LinkRefused, match="not one of them"):
            _fetch(links.url("/d", host="localhost"), [origin_for(links)])
        assert links.requests == []

    def test_a_lookalike_host_is_refused(self) -> None:
        origin = McpOrigin("kvasir", "https", "kvasir.example.org", 443)
        for url in (
            "https://kvasir.example.org.attacker.example/d",
            "https://evilkvasir.example.org/d",
            "https://example.org/d",
        ):
            with pytest.raises(LinkRefused):
                match_origin(url, [origin])

    def test_host_case_and_a_trailing_dot_still_match(self) -> None:
        origin = McpOrigin("kvasir", "https", "kvasir.example.org", 443)
        assert match_origin("https://KVASIR.Example.org./d", [origin]) is origin

    def test_another_port_on_the_host_is_refused(self, links: LinkServer) -> None:
        with pytest.raises(LinkRefused, match="scheme or port"):
            _fetch(f"http://127.0.0.1:{links.port + 1}/d", [origin_for(links)])

    def test_a_user_name_or_password_is_refused(self, links: LinkServer) -> None:
        with pytest.raises(LinkRefused, match="user name or password"):
            _fetch(f"http://user:pass@127.0.0.1:{links.port}/d", [origin_for(links)])

    def test_no_mcp_server_means_no_link(self, links: LinkServer) -> None:
        with pytest.raises(LinkRefused, match="connected to none"):
            _fetch(links.url("/d"), [])


class TestAddresses:
    """Rules 3 and 4: one lookup, pinned; private addresses only where the server is."""

    def test_the_lookup_is_made_once_and_the_connection_pinned(self, links: LinkServer) -> None:
        links.add("/d", Fixture(b"pinned", "text/plain"))
        calls: list[str] = []

        def resolver(host: str, port: int) -> list[str]:
            calls.append(host)
            return ["127.0.0.1"]

        origin = origin_for(links, host="docs.example.org")
        got = _fetch(f"http://docs.example.org:{links.port}/d", [origin], resolver=resolver)
        assert got.content == b"pinned"
        assert calls == ["docs.example.org"]
        # Connected to the pinned address, with the host's own name in Host.
        assert links.requests[-1]["headers"]["Host"] == f"docs.example.org:{links.port}"

    def test_a_private_address_the_server_is_not_at_is_refused(self, links: LinkServer) -> None:
        links.add("/d", Fixture(b"x", "text/plain"))
        origin = origin_for(links, addresses=frozenset({"192.0.2.10"}))
        with pytest.raises(LinkRefused, match="private or local address"):
            _fetch(links.url("/d"), [origin], resolver=lambda h, p: ["127.0.0.1"])
        assert links.requests == []

    def test_a_rebinding_to_loopback_is_refused(self, links: LinkServer) -> None:
        # The server's name resolved to a public address when its hosts were gathered;
        # the link's lookup now says loopback.
        origin = McpOrigin(
            "kvasir", "http", "kvasir.example.org", links.port, frozenset({"203.0.113.7"})
        )
        with pytest.raises(LinkRefused, match="private or local address"):
            _fetch(
                f"http://kvasir.example.org:{links.port}/d",
                [origin],
                resolver=lambda h, p: ["127.0.0.1"],
            )

    def test_a_public_address_is_preferred_over_a_private_one(self) -> None:
        origin = McpOrigin("k", "https", "k.example.org", 443, frozenset())
        pinned = guard._pin(origin, "k.example.org", 443, lambda h, p: ["10.0.0.5", "8.8.8.8"])
        assert pinned == "8.8.8.8"

    def test_an_unknown_host_is_refused(self, links: LinkServer) -> None:
        with pytest.raises(LinkRefused, match="could not be found"):
            _fetch(links.url("/d"), [origin_for(links)], resolver=lambda h, p: [])

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fe80::1",
            "fd00::1",
            "::ffff:127.0.0.1",
            "not an address",
        ],
    )
    def test_non_public_addresses(self, address: str) -> None:
        assert not is_public_address(address)

    @pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
    def test_public_addresses(self, address: str) -> None:
        assert is_public_address(address)


class TestRedirects:
    """Rule 5."""

    def test_a_redirect_to_another_host_is_refused(self, links: LinkServer) -> None:
        url = links.add("/d", Fixture(status=302, headers={"Location": "http://203.0.113.9/steal"}))
        with pytest.raises(LinkRefused, match="redirects to another host"):
            _fetch(url, [origin_for(links)])

    def test_a_redirect_to_another_name_on_the_same_port_is_refused(
        self, links: LinkServer
    ) -> None:
        links.add("/steal", Fixture(b"internal", "text/plain"))
        url = links.add(
            "/d", Fixture(status=302, headers={"Location": links.url("/steal", "localhost")})
        )
        with pytest.raises(LinkRefused, match="redirects to another host"):
            _fetch(url, [origin_for(links)])
        assert [r["path"] for r in links.requests] == ["/d"]

    def test_a_redirect_to_another_port_or_scheme_is_refused(self, links: LinkServer) -> None:
        for location in (
            f"http://127.0.0.1:{links.port + 1}/d2",
            f"https://127.0.0.1:{links.port}/d2",
        ):
            url = links.add("/d", Fixture(status=307, headers={"Location": location}))
            with pytest.raises(LinkRefused, match="redirects to another host"):
                _fetch(url, [origin_for(links)])

    def test_endless_redirects_are_refused(self, links: LinkServer) -> None:
        url = links.add("/loop", Fixture(status=302, headers={"Location": "/loop"}))
        with pytest.raises(LinkRefused, match="too many times"):
            _fetch(url, [origin_for(links)])
        assert len(links.requests) == guard.MAX_REDIRECTS + 1

    def test_an_error_answer_is_refused(self, links: LinkServer) -> None:
        with pytest.raises(LinkRefused, match="answered 404"):
            _fetch(links.url("/missing"), [origin_for(links)])


class TestSizeAndTime:
    """Rules 6 and 7."""

    def test_an_oversize_document_with_a_length_is_refused(self, links: LinkServer) -> None:
        url = links.add("/big", Fixture(b"a" * 2048, "text/plain"))
        with pytest.raises(LinkRefused, match="attachment limit of 1,024 bytes"):
            _fetch(url, [origin_for(links)], max_bytes=1024)

    def test_a_declared_oversize_length_is_refused_before_reading(self) -> None:
        class Unread(httpx.SyncByteStream):
            def __iter__(self) -> Any:
                raise AssertionError("the body was read")

        class Huge(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/plain", "Content-Length": str(10**9)},
                    stream=Unread(),
                )

        origin = McpOrigin("k", "https", "k.example.org", 443, frozenset({"10.0.0.8"}))
        with pytest.raises(LinkRefused, match="attachment limit of 1,024 bytes"):
            fetch(
                "https://k.example.org/d",
                [origin],
                max_bytes=1024,
                resolver=lambda h, p: ["10.0.0.8"],
                transport_factory=lambda verify: Huge(),
            )

    def test_an_oversize_document_without_a_length_is_refused(self, links: LinkServer) -> None:
        url = links.add("/big", Fixture(b"a" * 300_000, "text/plain", send_length=False))
        with pytest.raises(LinkRefused, match="attachment limit"):
            _fetch(url, [origin_for(links)], max_bytes=100_000)

    def test_a_document_at_the_limit_is_fetched(self, links: LinkServer) -> None:
        url = links.add("/ok", Fixture(b"a" * 1024, "text/plain"))
        assert len(_fetch(url, [origin_for(links)], max_bytes=1024).content) == 1024

    def test_a_slow_link_is_refused_in_time(self, links: LinkServer) -> None:
        url = links.add("/slow", Fixture(b"late", "text/plain", delay=3.0))
        started = time.monotonic()
        with pytest.raises(LinkRefused, match="did not answer in time"):
            _fetch(url, [origin_for(links)], timeout=0.5)
        assert time.monotonic() - started < 2.5

    def test_a_slow_drip_is_cut_off_at_the_limit(self, links: LinkServer) -> None:
        # Each part arrives inside the read timeout; the whole fetch must still end on time.
        url = links.add(
            "/drip", Fixture(b"a" * 40, "text/plain", send_length=False, parts=4, drip=0.8)
        )
        started = time.monotonic()
        with pytest.raises(LinkRefused, match="did not answer in time"):
            _fetch(url, [origin_for(links)], timeout=1.2)
        assert time.monotonic() - started < 1.9


class TestType:
    """Rule 8."""

    def test_a_type_that_does_not_match_the_content_is_refused(self, links: LinkServer) -> None:
        url = links.add("/d", Fixture(b"<html><script>x</script></html>", "application/pdf"))
        with pytest.raises(LinkRefused, match=r"declared type \(application/pdf\)"):
            _fetch(url, [origin_for(links)])

    def test_an_image_that_is_not_an_image_is_refused(self, links: LinkServer) -> None:
        url = links.add("/i", Fixture(pdf_bytes(), "image/png"))
        with pytest.raises(LinkRefused, match="does not match"):
            _fetch(url, [origin_for(links)])

    def test_a_compressed_answer_is_refused(self, links: LinkServer) -> None:
        url = links.add(
            "/z", Fixture(b"\x1f\x8b\x08", "text/plain", headers={"Content-Encoding": "gzip"})
        )
        with pytest.raises(LinkRefused, match="compressed"):
            _fetch(url, [origin_for(links)])


class TestNoCredentials:
    """Rule 9: only what the URL carries."""

    def test_only_the_three_headers_and_host_are_sent(
        self, links: LinkServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A proxy in the environment is not used (and would not be reachable).
        monkeypatch.setenv("HTTP_PROXY", "http://203.0.113.1:9")
        monkeypatch.setenv("HTTPS_PROXY", "http://203.0.113.1:9")
        url = links.add("/d?token=signed", Fixture(b"x", "text/plain"))
        links.fixtures["/d"] = Fixture(b"x", "text/plain")
        _fetch(url, [origin_for(links)])
        sent = {k.lower() for k in links.requests[-1]["headers"]}
        assert sent <= {"host", "user-agent", "accept", "accept-encoding", "connection"}
        assert links.requests[-1]["path"] == "/d?token=signed"

    def test_a_cookie_the_link_sets_is_not_sent_back(self, links: LinkServer) -> None:
        links.add("/final", Fixture(b"x", "text/plain"))
        url = links.add(
            "/start",
            Fixture(status=302, headers={"Location": "/final", "Set-Cookie": "session=abc"}),
        )
        _fetch(url, [origin_for(links)])
        assert all("Cookie" not in r["headers"] for r in links.requests)

    def test_https_keeps_the_host_name_for_tls(self) -> None:
        seen: list[httpx.Request] = []
        verify_seen: list[Any] = []

        class Capture(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                seen.append(request)
                return httpx.Response(
                    200, headers={"Content-Type": "text/plain"}, stream=httpx.ByteStream(b"hi")
                )

        def factory(verify: Any) -> httpx.BaseTransport:
            verify_seen.append(verify)
            return Capture()

        origin = McpOrigin("k", "https", "kvasir.example.org", 443, frozenset({"10.0.0.8"}))
        got = fetch(
            "https://kvasir.example.org/document-links/v1/t",
            [origin],
            max_bytes=MB,
            resolver=lambda h, p: ["10.0.0.8"],
            transport_factory=factory,
        )
        assert got.content == b"hi"
        request = seen[0]
        assert request.url.host == "10.0.0.8"
        assert request.headers["host"] == "kvasir.example.org"
        assert request.extensions["sni_hostname"] == "kvasir.example.org"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert verify_seen[0] not in (False, None)

    def test_a_server_without_certificate_checks_keeps_its_setting(self) -> None:
        verify_seen: list[Any] = []

        class Answer(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200, headers={"Content-Type": "text/plain"}, stream=httpx.ByteStream(b"")
                )

        def factory(verify: Any) -> httpx.BaseTransport:
            verify_seen.append(verify)
            return Answer()

        origin = McpOrigin(
            "k", "https", "k.example.org", 443, frozenset({"10.0.0.8"}), ssl_verify=False
        )
        fetch(
            "https://k.example.org/d",
            [origin],
            max_bytes=MB,
            resolver=lambda h, p: ["10.0.0.8"],
            transport_factory=factory,
        )
        assert verify_seen == [False]


class TestDispositionNames:
    @pytest.mark.parametrize(
        ("header", "name"),
        [
            ('inline; filename="a b.pdf"', "a b.pdf"),
            ("attachment; filename=plain.txt", "plain.txt"),
            ("attachment; filename*=UTF-8''caf%C3%A9.md", "café.md"),
            ('inline; filename="q\\"uote.txt"', 'q"uote.txt'),
            (None, None),
            ("inline", None),
        ],
    )
    def test_names(self, header: str | None, name: str | None) -> None:
        assert guard.disposition_filename(header) == name
