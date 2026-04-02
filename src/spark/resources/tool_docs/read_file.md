# Tool: read_file

## Purpose

Reads the contents of a text file and returns it as a string. This is the primary tool for reading source code, configuration files, logs, and other text-based files.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| path | string | Yes | - | Absolute path to the file to read |
| max_lines | integer | No | - | Maximum number of lines to return. If omitted, returns entire file |

## Return Value

The file content as text, prefixed with the file path. If `max_lines` is specified and the file exceeds that length, only the first N lines are returned with a note indicating truncation.

## Handling Large Files

**IMPORTANT**: This tool loads the file into memory. For large files:

1. **Check file size first**: Use `get_file_info` to check size before reading
2. **Use max_lines**: Set `max_lines` to limit output for large files
3. **Use find_in_file**: If searching for specific content

**Size Guidelines:**
| File Size | Recommended Approach |
|-----------|---------------------|
| < 100KB | Use `read_file` directly |
| 100KB - 1MB | Use `max_lines` to limit output |
| > 1MB | Use `find_in_file` or `max_lines` |

## Examples

### Basic File Read

```json
{
    "tool": "read_file",
    "input": {
        "path": "/project/README.md"
    }
}
```

### Read First 50 Lines

```json
{
    "tool": "read_file",
    "input": {
        "path": "/logs/application.log",
        "max_lines": 50
    }
}
```

## Best Practices

- Always check file size for unknown files before reading
- Use `max_lines` for log files or very large source files
- The path must be within the configured `allowed_paths`

## Common Pitfalls

- Reading very large files without `max_lines` can cause truncation or timeouts
- Paths outside `allowed_paths` will be denied
- Binary files will produce unreadable output

## Related Tools

- `list_directory` - List files in a directory
- `search_files` - Find files matching patterns
- `find_in_file` - Search within files
- `get_file_info` - Check file size and metadata
- `get_directory_tree` - View directory structure
