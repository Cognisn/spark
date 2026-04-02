# Tool: search_files

## Purpose

Searches for files matching name patterns across directories. Use this when you know part of a filename and need to locate it, or when searching for files following a naming convention.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| directory | string | Yes | - | Base directory to search in |
| pattern | string | Yes | - | Glob pattern to match (e.g., "*.py", "config*.json") |
| recursive | boolean | No | true | Search subdirectories |
| case_sensitive | boolean | No | true | Case-sensitive pattern matching |
| max_results | integer | No | 100 | Maximum number of results |

## Return Value

```json
{
    "pattern": "*.config.json",
    "directory": "/project",
    "matches": [
        "/project/app.config.json",
        "/project/test/test.config.json"
    ],
    "count": 2,
    "truncated": false
}
```

## Glob Pattern Syntax

| Pattern | Matches | Example |
|---------|---------|---------|
| `*` | Any characters | `*.py` matches `main.py` |
| `?` | Single character | `file?.txt` matches `file1.txt` |
| `[abc]` | Character set | `[abc].py` matches `a.py`, `b.py` |
| `[!abc]` | Not in set | `[!0-9].py` excludes numbered files |
| `**` | Any subdirectory | `**/*.py` matches all Python files |

## Handling Large Directories

1. **Be specific with patterns**: `test_*.py` is better than `*.py`
2. **Limit recursion**: Set `recursive: false` if files are in known location
3. **Use max_results**: Prevent overwhelming output
4. **Start narrow**: Begin with specific patterns, broaden if needed

## Examples

### Find All Python Files

```json
{
    "tool": "search_files",
    "input": {
        "directory": "/project",
        "pattern": "*.py"
    }
}
```

### Find Configuration Files

```json
{
    "tool": "search_files",
    "input": {
        "directory": "/project",
        "pattern": "*.config.*"
    }
}
```

### Case-Insensitive Search

```json
{
    "tool": "search_files",
    "input": {
        "directory": "/project",
        "pattern": "readme*",
        "case_sensitive": false
    }
}
```

### Non-Recursive Search

```json
{
    "tool": "search_files",
    "input": {
        "directory": "/project/src",
        "pattern": "*.py",
        "recursive": false
    }
}
```

## Best Practices

- Use specific patterns to reduce result count
- Combine with `read_file_text` to examine matching files
- Check `truncated` flag to know if more results exist
- Use `**` pattern carefully as it can match many files

## Common Pitfalls

- Forgetting that `*` doesn't match directory separators
- Pattern `*.py` won't find `.py` files in subdirectories without `recursive: true`
- Case sensitivity varies by operating system

## Related Tools

- `list_files_recursive` - List all files with more metadata
- `find_files_by_name` - Search by exact or partial filename
- `search_in_directory` - Search file contents rather than names
- `read_file_text` - Read files found by search
