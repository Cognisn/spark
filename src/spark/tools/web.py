"""Web tools — search and fetch web content.

Supports multiple search engines:
- DuckDuckGo (default, no API key)
- Brave Search (requires API key)
- Google via SerpAPI (requires API key)
- Bing via Azure (requires API key)
- SearXNG (self-hosted, requires instance URL)
"""

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
                "max_length": {
                    "type": "integer",
                    "description": "Max content length in chars. Default: 50000.",
                },
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
        return _web_search(tool_input["query"], tool_input.get("max_results", 5), config)
    elif tool_name == "web_fetch":
        return _web_fetch(tool_input["url"], tool_input.get("max_length", 50000))

    return f"Unknown web tool: {tool_name}"


# ---------------------------------------------------------------------------
# Search engine dispatcher
# ---------------------------------------------------------------------------


def _web_search(query: str, max_results: int, config: dict[str, Any]) -> str:
    """Route search to the configured engine."""
    web_config = config.get("embedded_tools", {}).get("web", {})
    engine = (web_config.get("search_engine") or "duckduckgo").lower().strip()

    engines = {
        "duckduckgo": _search_duckduckgo,
        "brave": _search_brave,
        "google": _search_google,
        "bing": _search_bing,
        "searxng": _search_searxng,
    }

    search_fn = engines.get(engine)
    if not search_fn:
        return f"Unknown search engine: {engine}. Available: {', '.join(engines.keys())}"

    return search_fn(query, max_results, web_config)


# ---------------------------------------------------------------------------
# DuckDuckGo (default — no API key)
# ---------------------------------------------------------------------------


def _search_duckduckgo(query: str, max_results: int, web_config: dict) -> str:
    """Search via DuckDuckGo HTML (no API key required)."""
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
        return f"Search results for '{query}' (DuckDuckGo):\n\n" + "\n\n".join(results)

    except Exception as e:
        logger.error("DuckDuckGo search failed: %s", e)
        return f"DuckDuckGo search failed: {e}"


# ---------------------------------------------------------------------------
# Brave Search (API key required — free tier available)
# ---------------------------------------------------------------------------


def _search_brave(query: str, max_results: int, web_config: dict) -> str:
    """Search via Brave Search API."""
    api_key = web_config.get("brave_api_key", "")
    if not api_key:
        return "Brave Search requires an API key. Set it in Settings > Embedded Tools > Web."

    try:
        import httpx

        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("web", {}).get("results", [])[:max_results]:
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("description", "")
            results.append(f"**{title}**\n{url}\n{snippet}")

        if not results:
            return f"No results found for: {query}"
        return f"Search results for '{query}' (Brave):\n\n" + "\n\n".join(results)

    except Exception as e:
        logger.error("Brave search failed: %s", e)
        return f"Brave search failed: {e}"


# ---------------------------------------------------------------------------
# Google via SerpAPI (API key required)
# ---------------------------------------------------------------------------


def _search_google(query: str, max_results: int, web_config: dict) -> str:
    """Search via Google using SerpAPI."""
    api_key = web_config.get("google_api_key", "")
    if not api_key:
        return "Google Search requires a SerpAPI key. Set it in Settings > Embedded Tools > Web."

    try:
        import httpx

        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={
                "q": query,
                "num": max_results,
                "api_key": api_key,
                "engine": "google",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("organic_results", [])[:max_results]:
            title = item.get("title", "")
            url = item.get("link", "")
            snippet = item.get("snippet", "")
            results.append(f"**{title}**\n{url}\n{snippet}")

        if not results:
            return f"No results found for: {query}"
        return f"Search results for '{query}' (Google):\n\n" + "\n\n".join(results)

    except Exception as e:
        logger.error("Google search failed: %s", e)
        return f"Google search failed: {e}"


# ---------------------------------------------------------------------------
# Bing via Azure Cognitive Services (API key required)
# ---------------------------------------------------------------------------


def _search_bing(query: str, max_results: int, web_config: dict) -> str:
    """Search via Bing Web Search API (Azure)."""
    api_key = web_config.get("bing_api_key", "")
    if not api_key:
        return "Bing Search requires an Azure API key. Set it in Settings > Embedded Tools > Web."

    try:
        import httpx

        resp = httpx.get(
            "https://api.bing.microsoft.com/v7.0/search",
            params={"q": query, "count": max_results},
            headers={"Ocp-Apim-Subscription-Key": api_key},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("webPages", {}).get("value", [])[:max_results]:
            title = item.get("name", "")
            url = item.get("url", "")
            snippet = item.get("snippet", "")
            results.append(f"**{title}**\n{url}\n{snippet}")

        if not results:
            return f"No results found for: {query}"
        return f"Search results for '{query}' (Bing):\n\n" + "\n\n".join(results)

    except Exception as e:
        logger.error("Bing search failed: %s", e)
        return f"Bing search failed: {e}"


# ---------------------------------------------------------------------------
# SearXNG (self-hosted, no API key — requires instance URL)
# ---------------------------------------------------------------------------


def _search_searxng(query: str, max_results: int, web_config: dict) -> str:
    """Search via a SearXNG instance."""
    instance_url = (web_config.get("searxng_url") or "").rstrip("/")
    if not instance_url:
        return "SearXNG requires an instance URL. Set it in Settings > Embedded Tools > Web."

    try:
        import httpx

        resp = httpx.get(
            f"{instance_url}/search",
            params={
                "q": query,
                "format": "json",
                "pageno": 1,
            },
            headers={"User-Agent": "Spark/1.0 (Web Research Tool)"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("results", [])[:max_results]:
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("content", "")
            results.append(f"**{title}**\n{url}\n{snippet}")

        if not results:
            return f"No results found for: {query}"
        return f"Search results for '{query}' (SearXNG):\n\n" + "\n\n".join(results)

    except Exception as e:
        logger.error("SearXNG search failed: %s", e)
        return f"SearXNG search failed: {e}"


# ---------------------------------------------------------------------------
# Web fetch (shared across all engines)
# ---------------------------------------------------------------------------


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
