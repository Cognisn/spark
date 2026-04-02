# Tool: get_current_datetime

## Purpose

Retrieves the current date and time, with support for different formats and timezones. Use this tool when you need to know the current time, format dates for documents, or work with timezone-aware timestamps.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| format | string | No | "iso" | Output format: "iso", "date", "time", "datetime", "unix", or custom strftime pattern |
| timezone | string | No | "local" | Timezone name (e.g., "UTC", "Australia/Sydney", "America/New_York") |

## Return Value

```json
{
    "datetime": "2026-03-04T14:30:00+11:00",
    "format": "iso",
    "timezone": "Australia/Sydney"
}
```

## Format Options

| Format | Example Output | Description |
|--------|----------------|-------------|
| iso | 2026-03-04T14:30:00+11:00 | ISO 8601 format with timezone |
| date | 2026-03-04 | Date only (YYYY-MM-DD) |
| time | 14:30:00 | Time only (HH:MM:SS) |
| datetime | 2026-03-04 14:30:00 | Date and time without timezone |
| unix | 1772789400 | Unix timestamp (seconds since epoch) |
| Custom | Mon, 04 Mar 2026 | Any strftime pattern (e.g., "%a, %d %b %Y") |

## Examples

### Basic Usage - Current Time

```json
{
    "tool": "get_current_datetime",
    "input": {}
}
```

**Result:**
```json
{
    "datetime": "2026-03-04T14:30:00+11:00",
    "format": "iso",
    "timezone": "local"
}
```

### Specific Timezone

```json
{
    "tool": "get_current_datetime",
    "input": {
        "timezone": "America/New_York"
    }
}
```

### Custom Format for Documents

```json
{
    "tool": "get_current_datetime",
    "input": {
        "format": "%d %B %Y",
        "timezone": "Australia/Sydney"
    }
}
```

**Result:**
```json
{
    "datetime": "04 March 2026",
    "format": "%d %B %Y",
    "timezone": "Australia/Sydney"
}
```

## Best Practices

- Use ISO format when storing or comparing dates programmatically
- Use custom formats for human-readable output in documents
- Always specify timezone when working with users in different regions
- Use Unix timestamps for calculations and database storage

## Common Pitfalls

- Forgetting timezone differences can cause confusion in scheduled tasks
- Custom format strings must use Python strftime syntax (% codes)
- "local" timezone depends on the server's configured timezone

## Related Tools

- `create_word_document` - Often used together to add dates to documents
- `query_chat_context` - Can search for date-related content in conversations
