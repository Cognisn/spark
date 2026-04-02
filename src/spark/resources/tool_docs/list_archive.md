# Tool: list_archive

## Purpose

Lists the contents of a ZIP or TAR archive file without extracting it. Shows filenames, sizes, and structure within the archive.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| path | string | Yes | - | Path to the archive file (.zip, .tar, .tar.gz, .tgz, .tar.bz2) |

## Return Value

A formatted listing of archive contents including:
- File paths within the archive
- File sizes
- Total file count

## Supported Formats

- ZIP (.zip)
- TAR (.tar)
- Gzipped TAR (.tar.gz, .tgz)
- Bzip2 TAR (.tar.bz2)

## Examples

### List ZIP Contents

```json
{
    "tool": "list_archive",
    "input": {
        "path": "/downloads/project.zip"
    }
}
```

## Best Practices

- Always list contents before extracting to understand the archive structure
- Check for path traversal issues (files with `../` in their paths)

## Related Tools

- `extract_archive` - Extract archive contents (when extract mode is enabled)
