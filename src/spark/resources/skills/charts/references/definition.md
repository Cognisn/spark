# The chart definition

The `definition` of `create_chart` is a closed vocabulary. A field outside it is refused with a message that lists the allowed fields.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `type` | Yes | One of `bar`, `line`, `area`, `scatter`, `pie`, `heatmap`, `treemap`, `sankey`, `graph`, `radar`, `funnel`, `boxplot` |
| `data` | Yes | Inline `columns` and `rows`, or a file of the conversation by `file_id` |
| `encoding` | Yes | Which column plays which part, by type |
| `x_title` | No | Horizontal axis title (`bar`, `line`, `area`, `scatter`, `heatmap`, `boxplot`) |
| `y_title` | No | Vertical axis title (the same types) |
| `donut` | No | `true` draws a `pie` as a ring (`pie` only) |
| `stacked` | No | `true` stacks the series (`bar`, `line`, `area`) |
| `horizontal` | No | `true` draws horizontal bars (`bar` only) |

An option on a type that does not take it is refused, as is an axis title on a type without axes. Options must be `true` or `false`.

## Data

Two forms:

- Inline: `columns` is a list of column names and `rows` a list of rows, each a list with one cell per column. A cell is text, a number or `null`.
- A file: `file_id` is a CSV or Excel (.xlsx) file in this conversation, or one of the user's own files. `sheet` names an Excel sheet (the first by default). `columns` optionally picks the columns to use.

The four data fields are `columns`, `rows`, `file_id` and `sheet`.

```json
{
  "title": "Units sold by quarter",
  "definition": {
    "type": "bar",
    "data": {"columns": ["Quarter", "Units"], "rows": [["Q1", 22], ["Q2", 23], ["Q3", 33]]},
    "encoding": {"x": "Quarter", "y": "Units"}
  },
  "caption": "Sales rose through the year."
}
```

A file by id (the file must exist in the conversation, so this form is not run as an example):

```text
"data": {"file_id": 12, "sheet": "2026", "columns": ["Month", "Region", "Sales"]}
```

## Encodings

`encoding` names the column for each part the type uses. The parts are `x`, `y`, `series`, `size` and `colour`.

| Type | `x` | `y` | `series` | `size` | `colour` |
|---|---|---|---|---|---|
| `bar` | category | value; several columns give one series each | splits the rows into series | | |
| `line` | ordered category | as bar | as bar | | |
| `area` | ordered category | as bar | as bar | | |
| `scatter` | number | number | groups the points | sizes each point | |
| `pie` | slice label | slice value | | | |
| `heatmap` | column category | row category | | | value that colours each cell |
| `treemap` | rectangle label | rectangle size (more than zero) | parent group | | |
| `sankey` | source node | target node | | flow value (more than zero) | |
| `graph` | source node | target node | group of the source node | link weight (default 1 per row) | |
| `radar` | measure (one spoke each) | value; several columns give one shape each | makes the shapes | | |
| `funnel` | stage | value | | | |
| `boxplot` | group | raw values to summarise | | | |

Give either several `y` columns or a `series` column, not both. Required parts: `x` and `y` for every type, `colour` for `heatmap`, `size` for `sankey`.

## Limits

Past a limit the chart is refused with a message saying what to change: group, filter or summarise first.

| Limit | Value |
|---|---|
| Rows | 5,000 |
| Columns | 50 |
| Series (and network groups) | 8 |
| Categories on an axis, heatmap rows and columns, box plot groups | 100 |
| Pie and funnel slices | 8 |
| Treemap rectangles | 200 |
| Network and Sankey nodes | 200 |
| Network and Sankey links | 1,000 |
| Radar measures | 3 to 20 |
| A label (category, node, series, column name) | 80 characters |
| Axis title | 80 characters |
| Caption | 500 characters |
| Node labels on hover only, above | 40 nodes |

A label may not start with `image://` or `path://`.

## Tool parameters

| Parameter | Required | Meaning |
|---|---|---|
| `title` | Yes | Short and specific, no file extension, never "Untitled" (at most 120 characters) |
| `definition` | Yes | The definition above |
| `caption` | No | One or two sentences under the chart |

## Common refusals and the fix

- "The data has no column X. Its columns are: ...": use the column names it lists.
- "Column X must hold numbers; row N has ...": clean the value, or pick another column.
- A limit message: summarise the data, group the small items as "Other", or filter.
- "A sankey chart cannot have a cycle": use `graph`.
