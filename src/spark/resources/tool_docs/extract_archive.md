# Tool: extract_archive

## Purpose

Extracts the contents of an archive file to a specified directory. Supports ZIP, TAR, and compressed archives.

## Paths, Server Mode and Attachments

- In server mode, `path` is a key in the user's own file area, as `list_my_files` shows it (for example `uploads/<id>/report.pdf`). In personal mode it is a path inside the allowed folders.
- Files the user attached in chat are read with `read_my_file` and the file id from the attachment note; call that rather than this tool.
- In server mode a bare file name (no folder) that is not found is looked up among the user's own files. One match is read, and the result starts with `Read file <id>: <name> (<key>)`. Several matches are listed by file id and nothing is read; no match lists the user's most recent files. Either way, use `read_my_file` with the file id.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| archive_path | string | Yes | - | Path to the archive file |
| destination | string | Yes | - | Directory to extract to |
| files | array[string] | No | null | Specific files to extract (null = all) |
| overwrite | boolean | No | false | Overwrite existing files |
| preserve_permissions | boolean | No | true | Preserve file permissions (Unix) |

## Return Value

```json
{
    "archive_path": "/downloads/project.zip",
    "destination": "/workspace/project",
    "files_extracted": 25,
    "total_size": 1048576,
    "extracted_files": [
        "/workspace/project/README.md",
        "/workspace/project/src/main.py",
        "/workspace/project/src/utils.py"
    ]
}
```

## Examples

### Extract Entire Archive

```json
{
    "tool": "extract_archive",
    "input": {
        "archive_path": "/downloads/project.zip",
        "destination": "/workspace/extracted"
    }
}
```

### Extract Specific Files

```json
{
    "tool": "extract_archive",
    "input": {
        "archive_path": "/downloads/large_archive.zip",
        "destination": "/workspace/selected",
        "files": [
            "config/settings.yaml",
            "src/main.py"
        ]
    }
}
```

### Extract with Overwrite

```json
{
    "tool": "extract_archive",
    "input": {
        "archive_path": "/backups/latest.tar.gz",
        "destination": "/app/data",
        "overwrite": true
    }
}
```

### Extract TAR.GZ Archive

```json
{
    "tool": "extract_archive",
    "input": {
        "archive_path": "/downloads/release.tar.gz",
        "destination": "/opt/application"
    }
}
```

## Workflow

1. **Check archive contents**:
```json
{"tool": "list_archive_contents", "input": {"path": "/downloads/data.zip"}}
```

2. **Extract to destination**:
```json
{"tool": "extract_archive", "input": {"archive_path": "/downloads/data.zip", "destination": "/workspace/data"}}
```

3. **Verify extraction**:
```json
{"tool": "list_files_recursive", "input": {"directory": "/workspace/data"}}
```

## Best Practices

- Always list contents before extracting
- Ensure destination directory exists or will be created
- Use selective extraction for large archives
- Set `overwrite: false` to prevent accidental data loss
- Check available disk space before extracting large archives

## Common Pitfalls

- Extracting to a directory that already has files
- Not checking archive contents first
- Running out of disk space during extraction
- Path traversal in malicious archives (tool validates paths)

## Security Considerations

The tool validates extracted file paths to prevent:
- Path traversal attacks (../../../etc/passwd)
- Symbolic link exploits
- Extracting outside the destination directory

## Related Tools

- `list_archive_contents` - Preview before extracting
- `read_archive_file` - Read single file without extracting
- `list_files_recursive` - Verify extracted contents
- `create_directories` - Prepare extraction destination
