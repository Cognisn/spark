# Tool: read_word_document

## Purpose

Extracts text content and structure from Microsoft Word documents (.docx files). Retrieves paragraphs, tables, headers, footers, and document metadata.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| path | string | Yes | - | Path to the Word document |
| include_tables | boolean | No | true | Extract table content |
| include_headers_footers | boolean | No | true | Extract headers and footers |
| include_metadata | boolean | No | true | Include document properties |

## Return Value

```json
{
    "path": "/documents/report.docx",
    "metadata": {
        "title": "Quarterly Report",
        "author": "John Smith",
        "created": "2026-01-15T10:00:00",
        "modified": "2026-03-04T09:00:00",
        "pages": 15,
        "words": 3500
    },
    "content": {
        "paragraphs": [
            {"text": "Executive Summary", "style": "Heading 1"},
            {"text": "This report covers Q1 2026...", "style": "Normal"}
        ],
        "tables": [
            {
                "rows": 5,
                "columns": 3,
                "data": [
                    ["Metric", "Target", "Actual"],
                    ["Revenue", "$1M", "$1.2M"]
                ]
            }
        ],
        "headers": ["Quarterly Report - Confidential"],
        "footers": ["Page {PAGE} of {NUMPAGES}"]
    }
}
```

## Handling Large Documents

For large Word documents:

1. **Check metadata first**: Request with `include_tables: false` to get overview
2. **Page count matters**: Documents with 50+ pages may have extensive content
3. **Tables are verbose**: Large tables significantly increase response size
4. **Extract selectively**: Disable components you don't need

### Strategy for Large Documents

```json
// Step 1: Get overview without tables
{
    "tool": "read_word_document",
    "input": {
        "path": "/docs/large_report.docx",
        "include_tables": false,
        "include_headers_footers": false
    }
}

// Step 2: Get full content if needed
{
    "tool": "read_word_document",
    "input": {
        "path": "/docs/large_report.docx"
    }
}
```

## Examples

### Read Full Document

```json
{
    "tool": "read_word_document",
    "input": {
        "path": "/documents/report.docx"
    }
}
```

### Text Only (No Tables)

```json
{
    "tool": "read_word_document",
    "input": {
        "path": "/documents/manual.docx",
        "include_tables": false
    }
}
```

### Metadata Only

```json
{
    "tool": "read_word_document",
    "input": {
        "path": "/documents/template.docx",
        "include_headers_footers": false,
        "include_tables": false
    }
}
```

## Best Practices

- Check metadata (page count) for large documents
- Disable tables if only text content is needed
- Use for .docx files (not legacy .doc)
- Consider extracting specific sections if document is very large

## Common Pitfalls

- Legacy .doc files require conversion first
- Embedded images are not extracted (use for text content)
- Complex formatting may not be fully preserved
- Very large tables can produce verbose output

## Related Tools

- `create_word_document` - Create new Word documents
- `read_pdf_document` - For PDF files
- `get_file_stats` - Check file size before reading
