"""Web tools — search and fetch web content."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_TOOLS = [
    {
        "name": "web_search",
        "description": "Search the web for information.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "max_results": {"type": "integer", "description": "Max results. Default: 5."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch",
        "description": "Fetch and read a web page, converting HTML to readable text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch."},
                "max_length": {"type": "integer", "description": "Max content length in chars. Default: 50000."},
            },
            "required": ["url"],
        },
    },
]


def get_tools() -> list[dict[str, Any]]:
    """Return web tool definitions."""
    return list(_TOOLS)


def execute(tool_name: str, tool_input: dict[str, Any], config: dict[str, Any]) -> str:
    """Execute a web tool."""
    if tool_name == "web_search":
        return _web_search(tool_input["query"], tool_input.get("max_results", 5))
    elif tool_name == "web_fetch":
        return _web_fetch(tool_input["url"], tool_input.get("max_length", 50000))

    return f"Unknown web tool: {tool_name}"


def _web_search(query: str, max_results: int) -> str:
    """Search the web using DuckDuckGo (no API key required)."""
    try:
        import httpx

        resp = httpx.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Spark/1.0 (Web Research Tool)"},
            timeout=30,
            follow_redirects=True,
        )

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        for r in soup.select(".result"):
            title_el = r.select_one(".result__title a")
            snippet_el = r.select_one(".result__snippet")
            if title_el:
                title = title_el.get_text(strip=True)
                url = title_el.get("href", "")
                snippet = snippet_el.get_text(strip=True) if snippet_el else ""
                results.append(f"**{title}**\n{url}\n{snippet}")
                if len(results) >= max_results:
                    break

        if not results:
            return f"No results found for: {query}"
        return f"Search results for '{query}':\n\n" + "\n\n".join(results)

    except Exception as e:
        logger.error("Web search failed: %s", e)
        return f"Web search failed: {e}"


def _web_fetch(url: str, max_length: int) -> str:
    """Fetch a URL and convert to readable text."""
    try:
        import httpx

        resp = httpx.get(
            url,
            headers={"User-Agent": "Spark/1.0 (Web Research Tool)"},
            timeout=30,
            follow_redirects=True,
        )

        content_type = resp.headers.get("content-type", "")

        if "application/json" in content_type:
            return resp.text[:max_length]

        if "text/html" in content_type or not content_type:
            import html2text

            converter = html2text.HTML2Text()
            converter.ignore_links = False
            converter.ignore_images = True
            converter.body_width = 0
            text = converter.handle(resp.text)
            if len(text) > max_length:
                text = text[:max_length] + "\n... (truncated)"
            return text

        # Plain text
        return resp.text[:max_length]

    except Exception as e:
        logger.error("Web fetch failed: %s", e)
        return f"Web fetch failed: {e}"
