# Tool: get_file_info

## Purpose

Retrieves extended information about a file, including MIME type detection, encoding detection for text files, and hash values. More detailed than `get_file_stats`, useful for file validation and identification.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| path | string | Yes | - | Path to the file |
| compute_hash | boolean | No | false | Calculate file hash (MD5 and SHA256) |

## Return Value

```json
{
    "path": "/data/report.csv",
    "exists": true,
    "size": 2048576,
    "size_human": "2.0 MB",
    "mime_type": "text/csv",
    "encoding": "utf-8",
    "is_text": true,
    "is_binary": false,
    "created": "2026-03-01T10:00:00",
    "modified": "2026-03-04T09:30:00",
    "extension": ".csv",
    "md5": "d41d8cd98f00b204e9800998ecf8427e",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
}
```

## Use Cases

### File Type Detection
Determine the actual file type regardless of extension:
```json
{
    "tool": "get_file_info",
    "input": {
        "path": "/uploads/unknown_file"
    }
}
```

### Encoding Detection
Find the correct encoding for text files:
```json
{
    "tool": "get_file_info",
    "input": {
        "path": "/legacy/old_data.txt"
    }
}
```

Use the `encoding` result with `read_file_text`.

### File Integrity Verification
Compute hashes for verification:
```json
{
    "tool": "get_file_info",
    "input": {
        "path": "/downloads/package.zip",
        "compute_hash": true
    }
}
```

## Examples

### Basic File Info

```json
{
    "tool": "get_file_info",
    "input": {
        "path": "/project/data.json"
    }
}
```

### With Hash Computation

```json
{
    "tool": "get_file_info",
    "input": {
        "path": "/releases/app-v1.0.zip",
        "compute_hash": true
    }
}
```

## Best Practices

- Use `is_text` to determine if `read_file_text` is appropriate
- Use detected `encoding` when reading text files
- Only compute hashes when needed (adds processing time)
- Check `mime_type` for proper file handling

## Common Pitfalls

- Hash computation on large files can be slow
- MIME type detection is based on content, not just extension
- Encoding detection is a best guess; may not be 100% accurate

## Related Tools

- `get_file_stats` - Simpler metadata without MIME/encoding
- `read_file_text` - Read text files with detected encoding
- `read_file_binary` - Read binary files
