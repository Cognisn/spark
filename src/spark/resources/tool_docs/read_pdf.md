# Tool: read_pdf_document

## Purpose

Extracts text content from PDF documents. Handles multi-page PDFs with optional page range selection. Use for reading reports, papers, scanned documents (with OCR), and other PDF content.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| path | string | Yes | - | Path to the PDF file |
| pages | string | No | null | Page range (e.g., "1-5", "1,3,5", "2-") or null for all |
| include_metadata | boolean | No | true | Include document properties |

## Return Value

```json
{
    "path": "/documents/annual_report.pdf",
    "metadata": {
        "title": "Annual Report 2025",
        "author": "Finance Department",
        "created": "2026-01-15",
        "pages": 45,
        "producer": "Microsoft Word"
    },
    "content": [
        {
            "page": 1,
            "text": "Annual Report 2025\n\nTable of Contents\n1. Executive Summary...\n2. Financial Overview..."
        },
        {
            "page": 2,
            "text": "Executive Summary\n\nThis report presents the financial performance..."
        }
    ],
    "total_pages": 45,
    "pages_extracted": 2
}
```

## Page Range Syntax

| Syntax | Meaning | Example |
|--------|---------|---------|
| `"1-5"` | Pages 1 through 5 | First 5 pages |
| `"1,3,5"` | Specific pages | Pages 1, 3, and 5 |
| `"5-"` | Page 5 to end | Everything from page 5 |
| `"-10"` | First 10 pages | Pages 1-10 |
| `null` | All pages | Entire document |

## Handling Large PDFs

For PDFs with many pages:

1. **Check metadata first**: Use page count to plan extraction
2. **Use page ranges**: Extract only needed sections
3. **Extract incrementally**: Process in page batches
4. **Scanned PDFs**: May have slower OCR processing

### Large PDF Strategy

```json
// Step 1: Get page count
{
    "tool": "read_pdf_document",
    "input": {
        "path": "/docs/long_report.pdf",
        "pages": "1"
    }
}
// Check total_pages in response

// Step 2: Read table of contents (usually first pages)
{
    "tool": "read_pdf_document",
    "input": {
        "path": "/docs/long_report.pdf",
        "pages": "1-3"
    }
}

// Step 3: Read specific sections
{
    "tool": "read_pdf_document",
    "input": {
        "path": "/docs/long_report.pdf",
        "pages": "15-20"
    }
}
```

## Examples

### Read Entire PDF

```json
{
    "tool": "read_pdf_document",
    "input": {
        "path": "/documents/contract.pdf"
    }
}
```

### Read First 5 Pages

```json
{
    "tool": "read_pdf_document",
    "input": {
        "path": "/documents/manual.pdf",
        "pages": "1-5"
    }
}
```

### Read Specific Pages

```json
{
    "tool": "read_pdf_document",
    "input": {
        "path": "/documents/report.pdf",
        "pages": "1,5,10"
    }
}
```

### Read Without Metadata

```json
{
    "tool": "read_pdf_document",
    "input": {
        "path": "/documents/article.pdf",
        "include_metadata": false
    }
}
```

## Best Practices

- Check page count before reading large PDFs
- Use page ranges for targeted extraction
- First pages often contain table of contents
- Last pages often contain references/appendices

## Common Pitfalls

- Scanned PDFs require OCR and may have lower accuracy
- Complex layouts (multi-column) may have jumbled text order
- Password-protected PDFs cannot be read
- Embedded images are not extracted
- Very large PDFs may timeout; use page ranges

## Related Tools

- `read_word_document` - For Word documents
- `read_powerpoint_document` - For PowerPoint files
- `get_file_stats` - Check PDF file size before reading
