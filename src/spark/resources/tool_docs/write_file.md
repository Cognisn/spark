# Tool: write_file

## Purpose

Writes content to a file, creating it if it doesn't exist or overwriting if it does. Use for creating new files, saving generated content, updating configurations, or writing output files.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| path | string | Yes | - | Path where the file should be written |
| content | string | Yes | - | Content to write to the file |
| encoding | string | No | "utf-8" | Text encoding to use |
| create_dirs | boolean | No | false | Create parent directories if they don't exist |
| append | boolean | No | false | Append to existing file instead of overwriting |
| backup | boolean | No | false | Create backup of existing file before overwriting |

## Return Value

```json
{
    "path": "/project/output/report.txt",
    "bytes_written": 1024,
    "created": true,
    "backup_path": null
}
```

With backup:
```json
{
    "path": "/project/config.yaml",
    "bytes_written": 512,
    "created": false,
    "backup_path": "/project/config.yaml.bak"
}
```

## Examples

### Create New File

```json
{
    "tool": "write_file",
    "input": {
        "path": "/project/output/results.json",
        "content": "{\"status\": \"complete\", \"items\": 42}"
    }
}
```

### Create with Parent Directories

```json
{
    "tool": "write_file",
    "input": {
        "path": "/project/output/2026/03/report.txt",
        "content": "Monthly report data...",
        "create_dirs": true
    }
}
```

### Append to Existing File

```json
{
    "tool": "write_file",
    "input": {
        "path": "/logs/custom.log",
        "content": "2026-03-04 14:30:00 New entry\n",
        "append": true
    }
}
```

### Overwrite with Backup

```json
{
    "tool": "write_file",
    "input": {
        "path": "/project/config.yaml",
        "content": "database:\n  host: localhost\n  port: 5432",
        "backup": true
    }
}
```

### Write with Specific Encoding

```json
{
    "tool": "write_file",
    "input": {
        "path": "/data/legacy_format.txt",
        "content": "Data with special characters...",
        "encoding": "latin-1"
    }
}
```

## Safety Considerations

| Scenario | Recommended Approach |
|----------|---------------------|
| Overwriting important files | Use `backup: true` |
| Writing to new directories | Use `create_dirs: true` |
| Adding to logs | Use `append: true` |
| Critical configurations | Read first, confirm changes |

## Best Practices

- Use `backup: true` when overwriting important files
- Verify content before writing to avoid data loss
- Use appropriate encoding for the file type
- Consider using `append` for log-style files
- Check file exists first if overwriting is unintended

## Common Pitfalls

- Overwriting without backup loses original content
- Writing without `create_dirs` fails if parent doesn't exist
- Wrong encoding can corrupt special characters
- Large content strings may cause memory issues

## Related Tools

- `read_file_text` - Read existing file before modifying
- `create_directories` - Create directory structure first
- `get_file_stats` - Check if file exists before writing
- `diff_files` - Compare original and new versions
