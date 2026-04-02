# Tool: read_excel_document

## Purpose

Reads data from Excel files (.xlsx, .xls) with support for pagination, column selection, cell ranges, and filtering. Designed for efficient handling of large spreadsheets through selective data loading.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| path | string | Yes | - | Path to the Excel file |
| sheet | string/integer | No | 0 | Sheet name or index (0-based) |
| offset | integer | No | 0 | Starting row (0 = first data row after headers) |
| limit | integer | No | 100 | Maximum rows to return |
| columns | array[string] | No | null | Column names or letters to include (null = all) |
| cell_range | string | No | null | Specific cell range like "A1:D50" |
| include_headers | boolean | No | true | Include column headers in response |
| filter_column | string | No | null | Column to filter on |
| filter_value | string | No | null | Value to match in filter_column |

## Return Value

```json
{
    "path": "/data/sales.xlsx",
    "sheet": "Sales 2026",
    "headers": ["Date", "Region", "Product", "Revenue"],
    "data": [
        ["2026-01-15", "North", "Widget A", 1500.00],
        ["2026-01-16", "South", "Widget B", 2300.00],
        ["2026-01-17", "North", "Widget A", 1800.00]
    ],
    "rows_returned": 3,
    "total_rows": 50000,
    "has_more": true,
    "columns_returned": ["Date", "Region", "Product", "Revenue"]
}
```

## Column Selection

Columns can be specified by:
- **Header name**: `["Date", "Revenue", "Product"]`
- **Column letter**: `["A", "C", "E"]`
- **Mixed**: `["Date", "C", "Revenue"]`

Header names are checked first, then column letters (max 3 characters like "AA", "AB").

## Handling Large Excel Files

### Strategy Overview

| File Size | Rows | Recommended Approach |
|-----------|------|---------------------|
| < 1 MB | < 1,000 | Read directly with default settings |
| 1-10 MB | 1,000-10,000 | Use column selection, moderate pagination |
| 10-50 MB | 10,000-100,000 | Use structure inspection, pagination, search |
| > 50 MB | > 100,000 | Aggressive pagination, targeted reading |

### Step-by-Step Workflow

#### 1. Inspect Structure First
```json
{"tool": "get_excel_structure", "input": {"path": "/data/large.xlsx"}}
```

#### 2. Preview Data (First 20 Rows)
```json
{
    "tool": "read_excel_document",
    "input": {
        "path": "/data/large.xlsx",
        "limit": 20
    }
}
```

#### 3. Select Only Needed Columns
```json
{
    "tool": "read_excel_document",
    "input": {
        "path": "/data/large.xlsx",
        "columns": ["ID", "Name", "Status"],
        "limit": 100
    }
}
```

#### 4. Paginate Through Data
```json
// Page 1
{"tool": "read_excel_document", "input": {"path": "/data/large.xlsx", "offset": 0, "limit": 100}}

// Page 2
{"tool": "read_excel_document", "input": {"path": "/data/large.xlsx", "offset": 100, "limit": 100}}

// Page 3
{"tool": "read_excel_document", "input": {"path": "/data/large.xlsx", "offset": 200, "limit": 100}}
```

## Examples

### Basic Read

```json
{
    "tool": "read_excel_document",
    "input": {
        "path": "/data/report.xlsx"
    }
}
```

### Read Specific Sheet

```json
{
    "tool": "read_excel_document",
    "input": {
        "path": "/data/workbook.xlsx",
        "sheet": "Q1 Data"
    }
}
```

### Read with Pagination

```json
{
    "tool": "read_excel_document",
    "input": {
        "path": "/data/large_dataset.xlsx",
        "offset": 500,
        "limit": 50
    }
}
```

### Select Specific Columns

```json
{
    "tool": "read_excel_document",
    "input": {
        "path": "/data/wide_table.xlsx",
        "columns": ["ID", "Name", "Email", "Status"]
    }
}
```

### Read Cell Range

```json
{
    "tool": "read_excel_document",
    "input": {
        "path": "/data/report.xlsx",
        "cell_range": "B5:F25"
    }
}
```

### Filter by Column Value

```json
{
    "tool": "read_excel_document",
    "input": {
        "path": "/data/orders.xlsx",
        "filter_column": "Status",
        "filter_value": "Pending",
        "columns": ["OrderID", "Customer", "Status", "Total"]
    }
}
```

### Combined: Columns, Filter, and Pagination

```json
{
    "tool": "read_excel_document",
    "input": {
        "path": "/data/sales.xlsx",
        "columns": ["Date", "Region", "Revenue"],
        "filter_column": "Region",
        "filter_value": "North",
        "offset": 0,
        "limit": 50
    }
}
```

## Best Practices

1. **Always inspect first**: Use `get_excel_structure` for unknown files
2. **Select columns**: Only request columns you need
3. **Use pagination**: For files with 1000+ rows
4. **Filter early**: Use `filter_column` to reduce data
5. **Check `has_more`**: Know if more data exists
6. **Use `search_excel`**: Find data before reading ranges

## Common Pitfalls

- Reading entire large files without pagination
- Requesting all columns when only a few are needed
- Not checking sheet names (defaulting to wrong sheet)
- Forgetting that offset is 0-based (row after headers)
- Column names are case-sensitive for matching

## Related Tools

- `get_excel_structure` - **Always use first** for unknown files
- `search_excel` - Find values before reading
- `get_excel_column_stats` - Get statistics for columns
- `create_excel_document` - Create new Excel files
