# Tool: create_excel

## Purpose

Creates a Microsoft Excel (.xlsx) workbook with formatted sheets. Use for generating data reports, dashboards, financial summaries, inventories, and any structured tabular data.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| path | string | Yes | Output file path (must end in .xlsx, within allowed paths) |
| sheets | array | Yes | Array of sheet definitions |
| title | string | No | Workbook title metadata |
| author | string | No | Workbook author metadata |

### Sheet Definition

Each sheet in the `sheets` array supports:

| Property | Type | Description |
|----------|------|-------------|
| name | string | Required. Sheet tab name (max 31 characters) |
| headers | array | Column header labels (strings). Styled with bold white text on coloured background |
| rows | array | Data rows — each row is an array of values. Numbers are auto-detected |
| column_widths | array | Column widths in characters. Auto-sized if omitted |
| header_colour | string | Header background hex colour (default: "#4472C4" blue) |
| auto_filter | boolean | Enable dropdown filters on headers (default: true) |
| freeze_panes | string | Cell reference to freeze at e.g. "A2" freezes header row (default: "A2" when headers present) |

### Data Types

Values in rows are automatically detected:
- Integers: "42" → 42
- Floats: "3.14" → 3.14
- Strings: anything else stays as text
- Formulas: use Excel formula syntax e.g. "=SUM(B2:B10)"

## Examples

### Financial Report

```json
{
  "tool": "create_excel",
  "input": {
    "path": "/reports/financial_summary.xlsx",
    "title": "Financial Summary Q1 2026",
    "author": "Spark",
    "sheets": [
      {
        "name": "Revenue",
        "headers": ["Month", "Product A", "Product B", "Total"],
        "rows": [
          ["January", "45000", "32000", "=B2+C2"],
          ["February", "48000", "35000", "=B3+C3"],
          ["March", "52000", "38000", "=B4+C4"],
          ["Total", "=SUM(B2:B4)", "=SUM(C2:C4)", "=SUM(D2:D4)"]
        ],
        "column_widths": [15, 15, 15, 15],
        "header_colour": "#2E75B6"
      },
      {
        "name": "Expenses",
        "headers": ["Category", "Budget", "Actual", "Variance"],
        "rows": [
          ["Salaries", "120000", "118500", "=B2-C2"],
          ["Marketing", "25000", "27800", "=B3-C3"],
          ["Infrastructure", "15000", "14200", "=B4-C4"]
        ],
        "header_colour": "#C0504D"
      }
    ]
  }
}
```

### Data Export with Multiple Sheets

```json
{
  "tool": "create_excel",
  "input": {
    "path": "/exports/security_report.xlsx",
    "sheets": [
      {
        "name": "Incidents",
        "headers": ["ID", "Date", "Type", "Severity", "Status", "Description"],
        "rows": [
          ["INC-001", "2026-04-10", "SQL Injection", "Critical", "Resolved", "Attempted SQLi on login endpoint"],
          ["INC-002", "2026-04-11", "XSS", "High", "Open", "Reflected XSS in search parameter"]
        ],
        "column_widths": [10, 12, 15, 10, 10, 40]
      },
      {
        "name": "Summary",
        "headers": ["Metric", "Value"],
        "rows": [
          ["Total Incidents", "2"],
          ["Critical", "1"],
          ["High", "1"],
          ["Resolution Rate", "50%"]
        ],
        "column_widths": [20, 15]
      }
    ]
  }
}
```

### Simple Single-Sheet

```json
{
  "tool": "create_excel",
  "input": {
    "path": "/data/contacts.xlsx",
    "sheets": [
      {
        "name": "Contacts",
        "headers": ["Name", "Email", "Phone", "Company"],
        "rows": [
          ["Alice Smith", "alice@example.com", "+61 400 123 456", "Acme Corp"],
          ["Bob Jones", "bob@example.com", "+61 400 789 012", "Widget Inc"]
        ]
      }
    ]
  }
}
```

## Best Practices

- Use meaningful sheet names that describe the data
- Include headers for all sheets — they provide context and enable auto-filter
- Use formulas (=SUM, =AVERAGE, etc.) for calculated fields
- Set column widths explicitly for reports that will be printed or shared
- Use different header colours per sheet to visually distinguish them
- Keep sheet names under 31 characters (Excel limit)

## Common Pitfalls

- Path must be within allowed_paths
- Sheet names must be unique within the workbook
- Numbers passed as strings are auto-converted — use a leading apostrophe if you need text numbers
- Very large datasets (100K+ rows) may be slow to generate
- Formula syntax must match Excel format exactly

## Related Tools

- `read_excel` — Read existing Excel files
- `create_word` — Create Word documents for narrative reports
- `create_pdf` — Create PDF reports
