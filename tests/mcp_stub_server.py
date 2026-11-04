"""A local stub MCP server for tests: HTTP (streamable) and SSE on 127.0.0.1.

Run in a thread by :func:`serving`; it offers two tools, ``echo`` and ``fail``,
and records the ``Authorization`` and ``X-Key`` headers it receives so tests can
check credentials reached it. Synthetic only: no network beyond loopback.

The port comes from ``SPARK_TEST_MCP_PORT`` when set (a workstation slot),
otherwise the system picks a free one.
"""

from __future__ import annotations

import contextlib
import os
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount

HOST = "127.0.0.1"


@dataclass
class StubServer:
    port: int
    headers: list[dict[str, str]] = field(default_factory=list)

    @property
    def http_url(self) -> str:
        return f"http://{HOST}:{self.port}/http/mcp"

    @property
    def sse_url(self) -> str:
        return f"http://{HOST}:{self.port}/sse/sse"


def _free_port() -> int:
    configured = os.environ.get("SPARK_TEST_MCP_PORT")
    if configured:
        return int(configured)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def _app(stub: StubServer) -> Starlette:
    from mcp.server.mcpserver import MCPServer

    def build(name: str) -> Any:
        server = MCPServer(name)

        @server.tool()
        def echo(text: str) -> str:
            """Return the text given."""
            return f"echo: {text}"

        @server.tool()
        def fail() -> str:
            """Always fail."""
            raise RuntimeError("stub failure")

        return server

    http = build("stub-http")
    sse = build("stub-sse")
    http_app = http.streamable_http_app(host=HOST)
    sse_app = sse.sse_app(host=HOST)

    class Record:
        """Plain ASGI, so streamed responses pass through untouched."""

        def __init__(self, app: Any) -> None:
            self.app = app

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                wanted = (b"authorization", b"x-key")
                stub.headers.append(
                    {k.decode(): v.decode() for k, v in scope["headers"] if k in wanted}
                )
            await self.app(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        async with http.session_manager.run():
            yield

    return Starlette(
        routes=[Mount("/http", app=http_app), Mount("/sse", app=sse_app)],
        middleware=[Middleware(Record)],
        lifespan=lifespan,
    )


@contextlib.contextmanager
def serving() -> Iterator[StubServer]:
    """Serve the stub in a background thread for the duration of the block."""
    stub = StubServer(port=_free_port())
    config = uvicorn.Config(_app(stub), host=HOST, port=stub.port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mcp-stub", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the stub MCP server did not start")
        time.sleep(0.05)
    try:
        yield stub
    finally:
        server.should_exit = True
        thread.join(timeout=10)
