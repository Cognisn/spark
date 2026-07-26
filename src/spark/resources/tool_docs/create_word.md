# Tool: create_word

## Purpose

Creates a Microsoft Word (.docx) document with rich formatting. Use for generating reports, letters, documentation, proposals, and any structured text documents.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| path | string | Yes | Output file path (must end in .docx, within allowed paths) |
| content | array | Yes | Array of content blocks in document order |
| title | string | No | Document title metadata |
| author | string | No | Document author metadata |

### Content Block Types

Each block in the `content` array must have a `type` field. Available types:

#### heading
| Property | Type | Description |
|----------|------|-------------|
| type | "heading" | Required |
| text | string | Heading text |
| level | integer | 1 (largest) to 4 (smallest). Default: 1 |
| alignment | string | "left", "center", "right", "justify" |

#### paragraph
| Property | Type | Description |
|----------|------|-------------|
| type | "paragraph" | Required |
| text | string | Paragraph text |
| bold | boolean | Bold text |
| italic | boolean | Italic text |
| underline | boolean | Underlined text |
| colour | string | Hex colour e.g. "#FF0000" for red |
| font_size | integer | Font size in points (default ~11) |
| alignment | string | "left", "center", "right", "justify" |

#### table
| Property | Type | Description |
|----------|------|-------------|
| type | "table" | Required |
| rows | array | Array of row arrays e.g. [["H1","H2"],["A","B"]] |
| header_row | boolean | Bold first row as header (default: true) |

#### list
| Property | Type | Description |
|----------|------|-------------|
| type | "list" | Required |
| items | array | Array of list item strings |
| ordered | boolean | true = numbered, false = bullets (default) |

#### image
| Property | Type | Description |
|----------|------|-------------|
| type | "image" | Required |
| image_path | string | Path to image file (must be within allowed paths) |
| width_inches | number | Image width in inches (default: 5) |

#### page_break
| Property | Type | Description |
|----------|------|-------------|
| type | "page_break" | Required. Inserts a page break. |

## Examples

### Professional Report

```json
{
  "tool": "create_word",
  "input": {
    "path": "/documents/quarterly_report.docx",
    "title": "Q1 2026 Report",
    "author": "Spark",
    "content": [
      {"type": "heading", "text": "Quarterly Performance Report", "level": 1, "alignment": "center"},
      {"type": "paragraph", "text": "Q1 2026 — Confidential", "alignment": "center", "italic": true, "colour": "#666666"},
      {"type": "heading", "text": "Executive Summary", "level": 2},
      {"type": "paragraph", "text": "Revenue exceeded targets by 15% driven by strong growth in the enterprise segment."},
      {"type": "heading", "text": "Key Metrics", "level": 2},
      {"type": "table", "rows": [
        ["Metric", "Target", "Actual", "Status"],
        ["Revenue", "$1.0M", "$1.15M", "Above"],
        ["Users", "10,000", "12,500", "Above"],
        ["Churn", "< 5%", "3.2%", "On Track"]
      ]},
      {"type": "heading", "text": "Priorities for Q2", "level": 2},
      {"type": "list", "items": [
        "Launch enterprise self-service portal",
        "Expand into APAC market",
        "Reduce infrastructure costs by 20%"
      ]},
      {"type": "page_break"},
      {"type": "heading", "text": "Detailed Analysis", "level": 2},
      {"type": "paragraph", "text": "The following sections provide detailed breakdowns by segment..."}
    ]
  }
}
```

### Simple Letter

```json
{
  "tool": "create_word",
  "input": {
    "path": "/documents/letter.docx",
    "content": [
      {"type": "paragraph", "text": "13 April 2026", "alignment": "right"},
      {"type": "paragraph", "text": "Dear Client,"},
      {"type": "paragraph", "text": "Thank you for your enquiry. Please find below our proposal for the project scope discussed."},
      {"type": "paragraph", "text": "The total estimated cost is $25,000.", "bold": true},
      {"type": "paragraph", "text": "Kind regards,"},
      {"type": "paragraph", "text": "Matthew Westwood-Hill", "bold": true}
    ]
  }
}
```

## Best Practices

- Use heading levels consistently (H1 for title, H2 for sections, H3 for subsections)
- Use tables for structured data comparisons
- Use bullet lists for unordered items, numbered lists for sequential steps
- Set alignment to "center" for title pages
- Use page breaks to separate major sections
- Keep colour usage minimal and purposeful (e.g. red for warnings)

## Common Pitfalls

- Path must be within allowed_paths or the tool will deny access
- Image paths must also be within allowed paths and the file must exist
- Very large tables may affect document performance
- Font colours must be hex format with # prefix (e.g. "#FF0000")

## Related Tools

- `read_word` — Read existing Word documents
- `create_pdf` — Create PDF documents (similar content structure)
- `create_excel` — Create spreadsheets for tabular data
