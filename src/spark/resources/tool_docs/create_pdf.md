# Tool: create_pdf

## Purpose

Creates a PDF document with formatted content. Use for generating reports, invoices, certificates, and documents that need a fixed layout for printing or distribution.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| path | string | Yes | Output file path (must end in .pdf, within allowed paths) |
| content | array | Yes | Array of content blocks in document order |
| title | string | No | Document title (shown in PDF metadata and can be used in headers) |
| author | string | No | Document author metadata |
| page_size | string | No | "A4" (default) or "letter" |

### Content Block Types

#### heading
| Property | Type | Description |
|----------|------|-------------|
| type | "heading" | Required |
| text | string | Heading text |
| level | integer | 1 (largest, 18pt) to 4 (smallest, 11pt). Default: 1 |
| colour | string | Hex colour e.g. "#2E75B6" |

#### paragraph
| Property | Type | Description |
|----------|------|-------------|
| type | "paragraph" | Required |
| text | string | Paragraph text |
| bold | boolean | Bold text |
| italic | boolean | Italic text |
| colour | string | Hex colour |
| font_size | integer | Font size in points (default: 10) |
| alignment | string | "left" (default), "center", "right" |

#### table
| Property | Type | Description |
|----------|------|-------------|
| type | "table" | Required |
| rows | array | Array of row arrays. First row is styled as header (blue background, white text) |

Tables automatically have:
- Blue header row with white bold text
- Alternating row colours (white/light grey)
- Grid lines
- Automatic column sizing

#### list
| Property | Type | Description |
|----------|------|-------------|
| type | "list" | Required |
| items | array | Array of list item strings |
| ordered | boolean | true = numbered (1. 2. 3.), false = bullet points (default) |

#### image
| Property | Type | Description |
|----------|------|-------------|
| type | "image" | Required |
| image_path | string | Path to image file (must be within allowed paths) |
| width | number | Image width in inches (default: 5) |

#### page_break
| Property | Type | Description |
|----------|------|-------------|
| type | "page_break" | Starts a new page |

#### spacer
| Property | Type | Description |
|----------|------|-------------|
| type | "spacer" | Required |
| height | number | Vertical space in points (default: 12) |

## Examples

### Professional Report

```json
{
  "tool": "create_pdf",
  "input": {
    "path": "/reports/security_analysis.pdf",
    "title": "WAF Security Analysis Report",
    "author": "Spark Autonomous Action",
    "page_size": "A4",
    "content": [
      {"type": "heading", "text": "WAF Security Analysis Report", "level": 1, "colour": "#1F4E79"},
      {"type": "paragraph", "text": "Generated: 13 April 2026", "italic": true, "colour": "#666666"},
      {"type": "spacer", "height": 20},
      {"type": "heading", "text": "Executive Summary", "level": 2},
      {"type": "paragraph", "text": "This report provides a comprehensive analysis of web application firewall activity over the past 24 hours."},
      {"type": "heading", "text": "Attack Summary", "level": 2},
      {"type": "table", "rows": [
        ["Attack Type", "Count", "Blocked", "Status"],
        ["SQL Injection", "68", "68", "Fully Mitigated"],
        ["Cross-Site Scripting", "42", "42", "Fully Mitigated"],
        ["Path Traversal", "21", "21", "Fully Mitigated"]
      ]},
      {"type": "heading", "text": "Recommendations", "level": 2},
      {"type": "list", "ordered": true, "items": [
        "Update WAF rules to address new SQLi patterns identified in incident INC-2026-042",
        "Review rate limiting thresholds — current settings may be too permissive",
        "Schedule quarterly penetration test for Q2 2026"
      ]},
      {"type": "page_break"},
      {"type": "heading", "text": "Detailed Incident Analysis", "level": 2},
      {"type": "paragraph", "text": "The following sections provide detailed breakdowns of each incident category..."}
    ]
  }
}
```

### Invoice

```json
{
  "tool": "create_pdf",
  "input": {
    "path": "/documents/invoice_2026_04.pdf",
    "title": "Invoice #2026-04-001",
    "content": [
      {"type": "heading", "text": "INVOICE", "level": 1},
      {"type": "paragraph", "text": "Invoice #2026-04-001", "bold": true, "font_size": 14},
      {"type": "paragraph", "text": "Date: 13 April 2026"},
      {"type": "spacer", "height": 20},
      {"type": "paragraph", "text": "Bill To:", "bold": true},
      {"type": "paragraph", "text": "Acme Corporation\n123 Business Street\nSydney NSW 2000"},
      {"type": "spacer", "height": 20},
      {"type": "table", "rows": [
        ["Description", "Qty", "Unit Price", "Total"],
        ["Consulting Services", "40 hrs", "$150.00", "$6,000.00"],
        ["Security Audit", "1", "$2,500.00", "$2,500.00"],
        ["", "", "Subtotal", "$8,500.00"],
        ["", "", "GST (10%)", "$850.00"],
        ["", "", "Total Due", "$9,350.00"]
      ]},
      {"type": "spacer", "height": 30},
      {"type": "paragraph", "text": "Payment Terms: Net 30 days", "bold": true},
      {"type": "paragraph", "text": "Bank: Commonwealth Bank | BSB: 062-000 | Account: 12345678"}
    ]
  }
}
```

## Best Practices

- Use heading levels consistently for document structure
- Use tables for data that needs to be compared side-by-side
- Add spacers between major sections for readability
- Use page breaks before major new sections
- Keep colour usage consistent throughout the document
- For reports, include a title page with heading level 1, date, and author
- Use ordered lists for sequential steps, unordered for general items

## Common Pitfalls

- Path must be within allowed_paths
- Image paths must also be within allowed paths
- PDF tables auto-size columns — very wide tables may be compressed
- Hex colours must include the # prefix
- Very long paragraphs may span multiple pages (this is handled automatically)
- The reportlab library must be installed (included in Spark dependencies)

## Related Tools

- `read_pdf` — Read existing PDF files
- `create_word` — Create Word documents (editable alternative to PDF)
- `create_excel` — Create spreadsheets for raw data
- `send_email` — Email the generated PDF as an attachment
