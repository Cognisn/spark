# create_diagram

Draw a Mermaid diagram in the conversation and keep it as an artefact.

## Purpose

Shows a process, a structure or a timeline as a diagram the whole conversation can see: flowcharts, sequence, class, state and entity relationship diagrams, Gantt charts, timelines, mind maps and the other Mermaid types. SPARK draws it with Mermaid at its strict security level, with HTML labels off, in the conversation's light or dark theme.

The diagram is drawn in the message, and its toolbar opens it full size in the preview pane and saves it as PNG or SVG (with its title, on white by default). It is kept as a file of the conversation in your file area and counts towards your storage quota.

A Mermaid block written in an ordinary reply is still drawn in the message, but only this tool keeps a diagram with a title.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| title | string | Yes | A short, specific title such as "Release process" (no file extension, at most 120 characters) |
| source | string | Yes | The Mermaid text of the diagram, starting with its type (for example `flowchart TD`), at most 20,000 characters |
| caption | string | No | One or two sentences shown under the diagram (at most 500 characters) |

The source may not carry a `%%{init}%%` directive or a `config` block: SPARK sets how diagrams are drawn. A `title` block in front matter is fine.

## Return Value

A sentence naming the diagram and the file id it is kept as, then a line beginning `[spark-preview]` that the chat page draws as the diagram. A source that is missing, too long or changes Mermaid's settings is refused with the reason.

## Examples

```json
{
  "title": "Release process",
  "source": "flowchart TD\n  A[Merge] --> B{Gate green?}\n  B -->|yes| C[Stamp]\n  B -->|no| D[Fix]\n  D --> A",
  "caption": "Every merge waits for the gate."
}
```

## Best Practices

- Keep labels short; quote labels that contain punctuation: `A["Sign in (SSO)"]`.
- Use a chart (`create_chart`) for numbers and a diagram for steps, structure or time.

## Related Tools

- `create_chart` - Draw a chart from data
