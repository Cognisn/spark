# Web Search

Spark includes two web tools: `web_search` for searching the web and `web_fetch` for fetching and reading web pages. Multiple search engine backends are supported.

## Search Engines

| Engine | API Key Required | Notes |
|--------|-----------------|-------|
| **DuckDuckGo** | No | Default. Scrapes HTML results. No rate limit. |
| **Brave Search** | Yes (free tier) | REST API. Free tier at [search.brave.com/api](https://search.brave.com/api). |
| **Google (SerpAPI)** | Yes | Via [serpapi.com](https://serpapi.com). Paid API with free trial. |
| **Bing (Azure)** | Yes | Azure Cognitive Services Bing Search API key. |
| **SearXNG** | No (self-hosted) | Requires a running [SearXNG](https://docs.searxng.org/) instance. |

## Configuration

### Via Settings UI

Go to **Settings > Embedded Tools > Web** and:

1. Select the search engine from the dropdown
2. Enter the API key (if required)
3. For SearXNG, enter the instance URL

### Via config.yaml

```yaml
embedded_tools:
  web:
    enabled: true
    search_engine: duckduckgo      # duckduckgo, brave, google, bing, searxng
    brave_api_key: ""              # Brave Search API key
    google_api_key: ""             # SerpAPI key
    bing_api_key: ""               # Azure Bing Search key
    searxng_url: ""                # SearXNG instance URL
```

## DuckDuckGo (Default)

No API key required. Spark scrapes the DuckDuckGo HTML search page directly. This is the simplest option but may be subject to rate limiting if used heavily.

## Brave Search

1. Sign up for a free API key at [search.brave.com/api](https://search.brave.com/api)
2. The free tier allows 2,000 queries per month
3. Set `search_engine: brave` and provide `brave_api_key`

## Google (via SerpAPI)

1. Sign up at [serpapi.com](https://serpapi.com)
2. SerpAPI provides a Google search API with structured JSON results
3. Set `search_engine: google` and provide `google_api_key`

## Bing (via Azure)

1. Create a Bing Search resource in the [Azure Portal](https://portal.azure.com)
2. Get the API key from the resource's Keys section
3. Set `search_engine: bing` and provide `bing_api_key`

## SearXNG (Self-Hosted)

[SearXNG](https://docs.searxng.org/) is a privacy-focused metasearch engine you host yourself.

1. Deploy a SearXNG instance (Docker is recommended)
2. Ensure JSON output is enabled in SearXNG settings
3. Set `search_engine: searxng` and provide `searxng_url` (e.g., `https://search.example.com`)

## Web Fetch

The `web_fetch` tool fetches a URL and converts HTML to readable text using `html2text`. It handles:

- **HTML pages** -- Converted to readable markdown-like text
- **JSON responses** -- Returned as-is
- **Plain text** -- Returned as-is

The `max_length` parameter (default: 50,000 characters) truncates long pages.

## Usage

The web tools are used by the AI automatically when your query requires web information. You can also explicitly ask:

- "Search the web for recent Python 3.13 features"
- "Fetch the contents of https://example.com"
- "Look up the latest news about..."

The search tool returns the top results with title, URL, and snippet. The AI can then use `web_fetch` to read specific pages in detail.
