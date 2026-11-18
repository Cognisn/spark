"""A local stub MCP server spoken to over stdio, for personal-mode tests.

Run as ``python tests/mcp_stdio_stub.py``. It offers ``echo`` and ``env``, which
returns the value of the environment variable it is asked for, so a test can see
what the client passed to the subprocess. Synthetic only: no network.
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

server = MCPServer("stub-stdio")


@server.tool()
def echo(text: str) -> str:
    """Return the text given."""
    return f"echo: {text}"


@server.tool()
def env(name: str) -> str:
    """Return an environment variable's value (empty when unset)."""
    return os.environ.get(name, "")


if __name__ == "__main__":
    server.run("stdio")
