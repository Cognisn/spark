# Charts and Diagrams

Ask the assistant for a chart or a diagram and it draws one in the conversation. Charts are drawn with Apache ECharts and diagrams with Mermaid, both served by SPARK itself. Each one is kept as an artefact of the conversation, with a title, and every member of a shared conversation sees it.

## What the assistant can draw

### Charts

The assistant describes a chart with a SPARK chart definition and the `create_chart` tool. It never writes chart code: SPARK checks the definition and builds the chart itself, so nothing the assistant writes runs in your browser.

| Type | Shows |
|------|-------|
| Bar | Values per category, side by side, stacked or horizontal |
| Line | Change across ordered categories such as months |
| Area | Filled lines; stacked areas show parts of a total |
| Scatter | Points by two numbers, optionally sized by a third |
| Pie and donut | Parts of one whole |
| Heatmap | A grid of two categories coloured by a value |
| Treemap | Nested rectangles sized by value, optionally grouped |
| Sankey | Flows from sources to targets |
| Network graph | Nodes and the links between them, optionally grouped |
| Radar | Several measures on spokes, one shape per series |
| Funnel | Stages that narrow |
| Box plot | The spread of values per group: quartiles, whiskers and outliers |

The data is either written into the definition or read from a CSV or Excel (.xlsx) file in the conversation, such as a spreadsheet you attached. The assistant can also chart one of your own files by its id. A file someone else keeps outside the conversation cannot be charted: SPARK answers as if it did not exist.

Charts use SPARK's light and dark themes and a colour-blind-safe palette.

### Diagrams

The `create_diagram` tool draws a Mermaid diagram: flowcharts, sequence, class, state and entity relationship diagrams, Gantt charts, timelines, mind maps and the other Mermaid types. Diagrams are drawn at Mermaid's strict security level with HTML labels off. A Mermaid block in an ordinary reply is still drawn in the message, but only `create_diagram` keeps a diagram as an artefact with a title.

## Limits

| Limit | Value |
|-------|-------|
| Rows of data | 5,000 |
| Columns | 50 |
| Series | 8 |
| Categories on an axis | 100 |
| Pie and funnel slices | 8 |
| Treemap rectangles | 200 |
| Network and Sankey nodes | 200 |
| Network and Sankey links | 1,000 |
| Radar measures | 3 to 20 |
| A label | 80 characters |
| A title | 120 characters |
| A caption | 500 characters |
| A data file | 20 MB |
| A diagram's Mermaid text | 20,000 characters |

A chart past a limit is not drawn: the assistant is told which limit and asked to group, filter or summarise the data first, so a chart never shows part of its data without saying so. Network and Sankey charts with more than 40 nodes show node labels when you hover over them.

Charts and diagrams are kept in your file area, with a snapshot of the data a chart uses, and count towards your storage quota.

## In the conversation

A chart or diagram appears in the message at a fixed height, with its title and caption. Hover over a chart for its values, and click a legend entry to hide or show a series. The toolbar above it has:

- **Open in preview**: the chart or diagram full size in the preview pane beside the conversation. At phone width the pane covers the screen.
- **Save as PNG**: an image at twice the screen resolution.
- **Save as SVG**: a vector image that stays sharp at any size.
- **Download data** (charts only): the chart's data as a CSV file. A cell that a spreadsheet would read as a formula is saved as text.
- **Match theme**: saved images are on a white background unless this is ticked, when they take the current light or dark theme.

Saved images carry the title (and the caption) above the chart, and files are named from the title. Every toolbar button can be reached with the keyboard; the arrow keys move along the toolbar.

## Names

Every artefact SPARK makes has a title: charts, diagrams and the documents `create_word`, `create_excel`, `create_powerpoint` and `create_pdf` create. A document shown in the preview keeps the title it was shown with, and a file you attach keeps its file name. The title is plain text on one line.

See [Tools](tools.md) for the tools and [Conversations](conversations.md#document-preview) for the preview pane.
