# Tool: find_in_file

## Purpose

Searches for text patterns within a file and returns matching lines with context. Use this to locate specific content in files without reading the entire file, especially useful for large files, logs, and codebases.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| path | string | Yes | - | Path to the file to search |
| pattern | string | Yes | - | Text or regex pattern to search for |
| regex | boolean | No | false | Treat pattern as regular expression |
| case_sensitive | boolean | No | true | Case-sensitive matching |
| context_lines | integer | No | 2 | Number of lines before/after match to include |
| max_matches | integer | No | 50 | Maximum number of matches to return |

## Return Value

```json
{
    "path": "/project/app.py",
    "pattern": "def process_",
    "matches": [
        {
            "line_number": 45,
            "line": "def process_request(self, data):",
            "context_before": ["    # Process incoming request", ""],
            "context_after": ["        validated = self.validate(data)", "        return self.handle(validated)"]
        },
        {
            "line_number": 89,
            "line": "def process_response(self, result):",
            "context_before": ["", "    # Format response"],
            "context_after": ["        return json.dumps(result)", ""]
        }
    ],
    "total_matches": 2,
    "truncated": false
}
```

## Handling Large Files

This tool is **ideal for large files** because it:
1. Streams through the file without loading it entirely
2. Returns only matching lines with context
3. Limits results with `max_matches`

### Efficient Search Strategy

1. **Start broad**: Use a general pattern to find relevant areas
2. **Refine**: Use more specific patterns once you understand the file
3. **Use context**: Context lines help understand matches without re-reading

## Examples

### Simple Text Search

```json
{
    "tool": "find_in_file",
    "input": {
        "path": "/logs/error.log",
        "pattern": "ERROR"
    }
}
```

### Case-Insensitive Search

```json
{
    "tool": "find_in_file",
    "input": {
        "path": "/config/settings.yaml",
        "pattern": "database",
        "case_sensitive": false
    }
}
```

### Regex Pattern Search

```json
{
    "tool": "find_in_file",
    "input": {
        "path": "/project/app.py",
        "pattern": "def\\s+\\w+_handler\\(",
        "regex": true
    }
}
```

### Search with More Context

```json
{
    "tool": "find_in_file",
    "input": {
        "path": "/project/main.py",
        "pattern": "class Config",
        "context_lines": 5
    }
}
```

### Find Function Definitions

```json
{
    "tool": "find_in_file",
    "input": {
        "path": "/project/utils.py",
        "pattern": "^def ",
        "regex": true,
        "context_lines": 0
    }
}
```

## Regex Pattern Examples

| Pattern | Matches |
|---------|---------|
| `error\|warning` | Lines with "error" or "warning" |
| `^\s*#` | Comment lines (Python/shell) |
| `def \w+\(` | Function definitions |
| `\d{4}-\d{2}-\d{2}` | Dates in YYYY-MM-DD format |
| `TODO\|FIXME` | Todo/fixme comments |

## Best Practices

- Use regex for complex patterns but simple text for exact matches
- Set `max_matches` appropriately to avoid overwhelming results
- Use `context_lines: 0` when you just need line numbers
- Combine with `read_file_chunk` to read around specific matches

## Common Pitfalls

- Regex special characters (`.*+?[](){}^$\|`) need escaping in non-regex mode
- Very broad patterns in large files may hit max_matches quickly
- Context lines may include irrelevant content in dense files

## Related Tools

- `search_in_directory` - Search across multiple files
- `read_file_chunk` - Read specific portions after locating them
- `get_code_outline` - Better for understanding code structure
- `tail_file` - Better for reading recent log entries
