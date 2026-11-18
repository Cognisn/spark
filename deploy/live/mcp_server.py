"""A streamable-HTTP MCP server for the live end-to-end stack (test only).

    python -I /opt/spark-live/mcp_server.py

It runs from the SPARK image (which carries the ``mcp`` SDK) in the first web
replica's network namespace (``network_mode: service:web``), listening on
``127.0.0.1:8765``, so SPARK reaches it at ``http://127.0.0.1:8765/mcp``: plain
http only on loopback, which is conduit's rule for an MCP endpoint. It offers one
tool, ``live_echo``, whose answer the suite looks for in what the model is sent.
Synthetic only: it reads nothing and calls nothing.
"""

from __future__ import annotations

import uvicorn
from mcp.server.mcpserver import MCPServer

HOST = "127.0.0.1"
PORT = 8765

server = MCPServer("spark-live-mcp")


@server.tool()
def live_echo(text: str) -> str:
    """Return the text given, marked as coming from the live MCP server."""
    return f"live-mcp-echo: {text}"


def main() -> None:
    app = server.streamable_http_app(host=HOST)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
