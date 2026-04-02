# Tool: web_fetch

## Purpose

Fetch a web page and return its content as clean, readable text. The tool strips HTML markup, scripts, styles, and navigation elements, converting the page content to markdown-formatted text. Use this to read the full content of a specific URL, such as articles, documentation pages, or blog posts.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| url | string | Yes | - | The URL of the web page to fetch |
| max_length | integer | No | 50000 | Maximum content length in characters |

## Return Value

```json
{
    "success": true,
    "result": {
        "url": "https://example.com/article",
        "title": "Article Title",
        "content": "# Article Title\n\nThe article content converted to clean markdown...",
        "content_length": 12345,
        "content_type": "text/html",
        "status_code": 200,
        "truncated": false,
        "cached": false
    }
}
```

## Handling Large Pages

- The `max_length` parameter controls the maximum amount of text returned
- Content exceeding `max_length` is truncated and `truncated` is set to `true`
- For very large pages, consider using `web_extract` with `extract_type: "content"` to get just the main article text
- Default limit of 50,000 characters is suitable for most articles and documentation pages

## Examples

### Fetch a Web Page

```json
{
    "tool": "web_fetch",
    "input": {
        "url": "https://docs.python.org/3/tutorial/classes.html"
    }
}
```

### Fetch with Content Limit

```json
{
    "tool": "web_fetch",
    "input": {
        "url": "https://en.wikipedia.org/wiki/Machine_learning",
        "max_length": 20000
    }
}
```

### Workflow: Search then Fetch

1. Use `web_search` to find relevant pages
2. Use `web_fetch` to read the full content of the most relevant result

```json
{
    "tool": "web_fetch",
    "input": {
        "url": "https://example.com/relevant-result-from-search"
    }
}
```

## Best Practices

- Always check the `status_code` in the response — non-200 codes indicate issues
- Use `max_length` to avoid overwhelming context with very long pages
- Check `truncated` to know if content was cut short
- Results are cached for the configured TTL, so re-fetching the same URL is fast
- Prefer this tool over `web_extract` when you need the full readable text of a page
- Only text/html and text/plain content types are supported — binary files will return an error

## Common Pitfalls

- JavaScript-rendered content (SPAs) will not be available — the tool fetches raw HTML only
- Some sites block automated requests — these will return errors or empty content
- Very large pages should use a reduced `max_length` to avoid excessive context usage
- PDF, image, and other binary URLs are not supported — use filesystem tools for local files

## Related Tools

- `web_search` - Find URLs to fetch via web search
- `web_extract` - Extract structured data (tables, links, headings) from pages
