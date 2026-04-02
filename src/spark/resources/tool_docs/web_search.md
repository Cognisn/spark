# Tool: web_search

## Purpose

Search the web using a configured search provider (Brave, Google, or DuckDuckGo). Returns a list of search results with titles, URLs, and snippets. Use this tool to find current information, research topics, verify facts, or discover relevant web pages.

This tool works with all LLM providers (AWS Bedrock, Anthropic, Google Gemini, xAI/Grok, Ollama) regardless of whether the provider has native web search support.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| query | string | Yes | - | Search query describing what to find on the web |
| max_results | integer | No | 5 | Maximum number of results to return (1-10) |
| region | string | No | - | Region code for localised results (e.g. 'au', 'us', 'gb') |

## Return Value

```json
{
    "success": true,
    "result": {
        "query": "machine learning frameworks 2025",
        "provider": "brave",
        "result_count": 5,
        "results": [
            {
                "title": "Top ML Frameworks Compared",
                "url": "https://example.com/ml-frameworks",
                "snippet": "A comprehensive comparison of the leading machine learning frameworks...",
                "source": "example.com"
            }
        ],
        "cached": false
    }
}
```

## Search Providers

| Provider | API Key Required | Notes |
|----------|-----------------|-------|
| DuckDuckGo | No | Free, no registration needed. Good default option. |
| Brave | Yes | High-quality results, generous free tier. |
| Google | Yes (+ CX ID) | Google Custom Search Engine. Requires API key and CX ID. |

## Examples

### Basic Search

```json
{
    "tool": "web_search",
    "input": {
        "query": "Python FastAPI best practices"
    }
}
```

### Search with Region and Result Limit

```json
{
    "tool": "web_search",
    "input": {
        "query": "weather forecast Sydney",
        "max_results": 3,
        "region": "au"
    }
}
```

### Research Query

```json
{
    "tool": "web_search",
    "input": {
        "query": "CVE-2024 critical vulnerabilities summary",
        "max_results": 10
    }
}
```

## Best Practices

- Use specific, descriptive queries for better results
- Include relevant keywords and context in queries
- Use the `region` parameter when results should be location-specific
- Start with fewer results (3-5) and increase only if needed
- Follow up with `web_fetch` to read full content of relevant results
- Results are cached for the configured TTL, so repeated identical searches are fast

## Common Pitfalls

- Very broad queries return generic results — be specific
- DuckDuckGo may return fewer results than requested for niche queries
- Some search providers may rate-limit requests — use caching to reduce API calls
- The `region` parameter affects result ranking, not language filtering

## Related Tools

- `web_fetch` - Fetch and read the full content of URLs returned by search
- `web_extract` - Extract structured data (tables, links) from search result pages
