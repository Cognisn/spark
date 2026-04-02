# Tool: get_tool_documentation

## Purpose

Retrieves comprehensive documentation for any embedded tool. Use this when you need detailed information about a tool's parameters, return values, examples, or best practices before using it.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| tool_name | string | Yes | - | Name of the tool to get documentation for |

## Return Value

```json
{
    "tool_name": "read_excel_document",
    "documentation": "# Tool: read_excel_document\n\n## Purpose\n...",
    "available": true
}
```

If the tool is not found:
```json
{
    "tool_name": "unknown_tool",
    "documentation": null,
    "available": false,
    "error": "Documentation not found for tool: unknown_tool"
}
```

## Examples

### Get Documentation for a Tool

```json
{
    "tool": "get_tool_documentation",
    "input": {
        "tool_name": "read_excel_document"
    }
}
```

### Check Available Tools

To see what documentation is available, request the index:
```json
{
    "tool": "get_tool_documentation",
    "input": {
        "tool_name": "_index"
    }
}
```

## Best Practices

- Request documentation before using an unfamiliar tool
- Check the "Handling Large Data" section for files that may be large
- Review "Common Pitfalls" to avoid typical mistakes
- Look at "Related Tools" to discover complementary functionality

## Common Pitfalls

- Tool names are case-sensitive and use snake_case
- Some tools have similar names (e.g., `read_file_text` vs `read_file_chunk`)

## Related Tools

All embedded tools have documentation available through this tool.
