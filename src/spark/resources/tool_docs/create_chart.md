# create_chart

Draw a chart in the conversation from data, and keep it as an artefact.

## Purpose

Shows data as a chart the whole conversation can see. You describe the chart with a SPARK chart definition; SPARK checks it and draws it with Apache ECharts in SPARK's light and dark themes. Never write ECharts options, JavaScript or HTML: the definition is a closed vocabulary, and anything outside it is refused with a message saying what to change.

The chart is drawn in the message at a fixed height, with tooltips and a legend whose entries switch series on and off. Its toolbar opens it full size in the preview pane, saves it as PNG or SVG (with its title, on white by default) and downloads its data as CSV. It is kept as a file of the conversation in your file area (its definition and a snapshot of the data it uses) and counts towards your storage quota.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| title | string | Yes | A short, specific title such as "Monthly sign-ups, 2026" (no file extension, at most 120 characters). It names the chart and heads its saved images |
| definition | object | Yes | The chart definition (below) |
| caption | string | No | One or two sentences shown under the chart (at most 500 characters) |

## The definition

| Field | Required | Description |
|-------|----------|-------------|
| type | Yes | One of `bar`, `line`, `area`, `scatter`, `pie`, `heatmap`, `treemap`, `sankey`, `graph`, `radar`, `funnel`, `boxplot` |
| data | Yes | Inline `{"columns": [...], "rows": [[...], ...]}`, or a file `{"file_id": 12, "sheet": "Sales", "columns": ["Region", "Total"]}` |
| encoding | Yes | Which column plays each part: `x`, `y`, `series`, `size`, `colour`, by type (below) |
| x_title | No | Horizontal axis title (bar, line, area, scatter, heatmap, boxplot) |
| y_title | No | Vertical axis title (the same types) |
| donut | No | `true` draws a pie as a ring (pie only) |
| stacked | No | `true` stacks the series (bar, line, area) |
| horizontal | No | `true` draws horizontal bars (bar only) |

### Data

- **Inline:** `columns` names each column; `rows` holds the rows, each a list with one cell per column. A cell is text, a number or `null`.
- **A file:** `file_id` is a CSV or Excel (.xlsx) file in this conversation, or one of your own from `list_my_files`. `sheet` names an Excel sheet (default: the first). `columns` optionally picks the columns to use. The first row of the file holds the column names. Old .xls files are refused: save them as .xlsx or CSV.

Numbers may be numbers or numeric text (`"1,234"` reads as 1234). Rows with the same category are added together. Only the columns the encoding uses are kept with the chart.

### Encodings by type

| Type | x | y | series | size | colour | Options |
|------|---|---|--------|------|--------|---------|
| bar | category | value; several columns give one series each | splits rows into series | | | stacked, horizontal |
| line | category (ordered, such as dates) | as bar | as bar | | | stacked |
| area | category | as bar | as bar | | | stacked |
| scatter | number | number | groups the points | sizes each point | | |
| pie | slice label | slice value (zero or more) | | | | donut |
| heatmap | column category | row category | | | value that colours each cell | |
| treemap | rectangle label | size (more than zero) | parent group | | | |
| sankey | source node | target node | | flow value (more than zero) | | |
| graph | source node | target node | group of the source node | link weight (default 1 per row) | | |
| radar | measure (one spoke each) | value (zero or more); several columns give one shape each | makes the shapes | | | |
| funnel | stage | value (zero or more) | | | | |
| boxplot | group | the raw values to summarise | | | | |

Give either several `y` columns or a `series` column, not both. A sankey chart cannot have a cycle (flows that lead back to where they started): use `graph` for a network.

## Limits

| Limit | Value |
|-------|-------|
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
| A data file | 20 MB |

Past a limit the chart is refused rather than cut short: group the smaller items (for example as "Other"), filter or summarise first. Network and Sankey charts with more than 40 nodes show node labels when you hover. A label may not start with `image://` or `path://`.

## Return Value

A sentence naming the chart and the file id it is kept as, then a line beginning `[spark-preview]` that the chat page draws as the chart. A definition SPARK cannot draw is refused with the reason, for example `type must be one of: bar, line, ...` or `The data has no column Month. Its columns are: Quarter, Sales.`

## Examples

### Stacked bars from inline data
```json
{
  "title": "Units sold by quarter",
  "definition": {
    "type": "bar",
    "data": {"columns": ["Quarter", "North", "South"],
             "rows": [["Q1", 10, 12], ["Q2", 14, 9], ["Q3", 18, 15]]},
    "encoding": {"x": "Quarter", "y": ["North", "South"]},
    "y_title": "Units",
    "stacked": true
  },
  "caption": "North overtook South in the third quarter."
}
```

### A line per region from an attached spreadsheet
```json
{
  "title": "Monthly sales by region",
  "definition": {
    "type": "line",
    "data": {"file_id": 12, "sheet": "2026"},
    "encoding": {"x": "Month", "y": "Sales", "series": "Region"}
  }
}
```

### A flow
```json
{
  "title": "Where visitors go",
  "definition": {
    "type": "sankey",
    "data": {"columns": ["From", "To", "People"],
             "rows": [["Search", "Visit", 300], ["Visit", "Sign up", 90], ["Visit", "Leave", 210]]},
    "encoding": {"x": "From", "y": "To", "size": "People"}
  }
}
```

## Best Practices

- Choose the simplest type that answers the question: bars to compare, lines for change over time, scatter for relationships, pie only for a few parts of one whole.
- Summarise large data before charting it, and keep series to a handful.
- Give a title that says what the chart shows, and a caption with the one thing to notice.
- For a process or structure rather than data, use `create_diagram`.

## Related Tools

- `create_diagram` - Draw a Mermaid diagram
- `list_my_files` - Find a file's id
- `show_document` - Show a chart again in the preview pane by its file id
