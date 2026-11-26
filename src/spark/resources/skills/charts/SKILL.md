---
name: charts
description: Draw charts and graphs from data with create_chart. Use when the user asks for a chart, graph, plot, visualisation or dashboard of figures, or when numbers would be clearer drawn than listed (bar, line, area, scatter, pie, heatmap, treemap, Sankey, network, radar, funnel and box plot).
version: 0.3.0
---

# Charts

SPARK draws charts for you. You never write ECharts options, JavaScript or HTML: you call `create_chart` with a short title, a chart definition and an optional caption, and SPARK checks the definition and draws the chart in the conversation. Every member of the conversation sees it and can open it full size, save it as PNG or SVG, or download its data as CSV.

## When a chart helps

- Use a chart when the user asks for one, or when a pattern is easier to see than to read: change over time, a comparison across categories, parts of a whole, a relationship between two numbers, a flow, a network.
- Do not chart one or two numbers, or data with no pattern to show. A sentence or a table is better.
- For steps, structure, or time as a plan (not numbers), use a diagram instead: see the `mermaid-diagrams` skill and `create_diagram`.
- Ask only when you cannot tell which numbers to chart. Otherwise choose sensibly and say what you chose.

## Choosing a type

| The user wants to see | Use |
|---|---|
| Values compared across categories | `bar` (add `horizontal` for long names) |
| Change over ordered categories, usually time | `line` |
| Change over time with volume, or parts adding to a total | `area` (with `stacked`) |
| The relationship between two numbers | `scatter` (`size` for a third number) |
| Parts of one whole, a few of them | `pie` (`donut` for a ring) |
| Two categories and a value in a grid | `heatmap` |
| A total split into nested parts | `treemap` |
| A quantity flowing from sources to targets | `sankey` |
| A network of links, loops allowed | `graph` |
| A few things scored on several measures | `radar` |
| Stages that narrow | `funnel` |
| The spread of values per group | `boxplot` |

When two types would do, take the plainer one. Each type has its own reference with its encodings, options and a working example: read `references/<type>.md` (for example `references/bar.md`) before you build a type you have not used in this conversation.

## Making the chart

1. Get the data. Either put it inline in `data` as `columns` and `rows`, or name a CSV or Excel file of the conversation with `file_id` (and `sheet` for Excel, and optionally `columns` to pick the columns). Find a file's id with `list_my_files`. Never retype a large file's rows: name the file.
2. Summarise first when the data is large or detailed. Group the small parts as "Other", filter, or aggregate. SPARK refuses a chart past its limits rather than cutting it short (see `references/definition.md`).
3. Choose the type and say which column plays which part in `encoding`: `x`, `y`, `series`, `size` and `colour`, as the type uses them. Column names must match the data exactly.
4. Add `x_title` and `y_title` for types with axes, with units where they help. Add `donut`, `stacked` or `horizontal` only for the types that take them.
5. Call `create_chart` with a `title`, the `definition` and, when it helps, a `caption`.
6. If SPARK refuses the definition, read the message, fix exactly what it names and call again. Do not tell the user it worked until it did.
7. After the chart is drawn, say in a sentence what it shows. Do not repeat the data.

## Titles and captions

- The `title` is required: short and specific, such as "Monthly sign-ups, 2026", with no file extension, and never "Untitled" or "Chart". It names the artefact in the conversation and heads the saved images.
- The `caption` is optional: one or two sentences under the chart, saying the one thing to notice.
- Change a chart by making a new one with a new title when the content differs; reuse a title only to replace the same chart.

## Reading data from conversation files

- A file's first row holds the column names. Numbers may be numbers or numeric text (`"1,234"` reads as 1234). Rows with the same category are added together.
- Old .xls files are refused: ask for .xlsx or CSV, or read the file yourself and pass the rows inline.
- If a column name is not found, SPARK lists the real columns: use those names.
- Only the columns the encoding uses are kept with the chart.

## Other artefacts

Documents you create (Word, Excel, PowerPoint, PDF) need titles in the same way: see `references/documents.md`.

## References

- `references/definition.md`: every field of the definition, the data forms, the encodings by type and the limits.
- `references/<type>.md`: one file for each of the twelve types.
- `references/documents.md`: titles for documents and how a chart relates to them.
