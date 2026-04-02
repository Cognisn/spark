# Tool: list_directory

## Purpose

Lists the contents of a directory, showing files and subdirectories with their types and sizes. Useful for exploring project structure and finding files.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| path | string | Yes | - | Absolute path to the directory to list |

## Return Value

A formatted listing showing each item in the directory with:
- Item name
- Type (file or directory)
- Size (for files)

## Examples

### List a Project Directory

```json
{
    "tool": "list_directory",
    "input": {
        "path": "/project/src"
    }
}
```

## Best Practices

- Use `get_directory_tree` for a recursive view of nested structures
- Use `search_files` when looking for specific file patterns
- Combine with `read_file` to explore and then read specific files

## Common Pitfalls

- Path must be within configured `allowed_paths`
- Very large directories may produce lengthy output

## Related Tools

- `get_directory_tree` - Recursive tree view
- `search_files` - Find files by pattern
- `read_file` - Read file contents
- `get_file_info` - Detailed file metadata
