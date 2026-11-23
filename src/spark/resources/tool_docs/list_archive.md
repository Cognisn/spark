# Tool: list_archive

## Purpose

Lists the contents of a ZIP or TAR archive file without extracting it. Shows filenames, sizes, and structure within the archive.

## Paths, Server Mode and Attachments

- In server mode, `path` is a key in the user's own file area, as `list_my_files` shows it (for example `uploads/<id>/report.pdf`). In personal mode it is a path inside the allowed folders.
- Files the user attached in chat are read with `read_my_file` and the file id from the attachment note; call that rather than this tool.
- In server mode a bare file name (no folder) that is not found is looked up among the user's own files. One match is read, and the result starts with `Read file <id>: <name> (<key>)`. Several matches are listed by file id and nothing is read; no match lists the user's most recent files. Either way, use `read_my_file` with the file id.

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
