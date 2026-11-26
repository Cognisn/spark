---
name: mermaid-diagrams
description: Draw diagrams with Mermaid and create_diagram. Use when the user asks for a diagram, flowchart, sequence diagram, class diagram, state machine, entity relationship diagram, Gantt chart, timeline, mind map or architecture sketch, or when a process, structure or plan is clearer drawn than described.
version: 0.3.0
---

# Mermaid diagrams

SPARK draws Mermaid diagrams. You write the diagram as Mermaid text and call `create_diagram` with a short title, the `source` and an optional `caption`. SPARK keeps it as an artefact of the conversation and the chat page draws it, in the conversation's light or dark theme. Every member of the conversation sees it and can open it full size or save it as PNG or SVG.

## Mermaid rather than a chart

- Use a diagram for steps, structure, relationships, states or a schedule: things with boxes and arrows, or a plan along a time line.
- Use a chart (the `charts` skill and `create_chart`) when the user has numbers to compare, trend or total. A Gantt chart is a plan, so it is a diagram; a bar of monthly sales is a chart. That is the line between Mermaid rather than a chart.
- Use plain text for a short list of steps. Draw only when the shape matters.
- A Mermaid block in an ordinary reply is also drawn, in the message. Only `create_diagram` keeps a diagram as a titled artefact the user can open and save, so use it whenever the diagram is the deliverable.

## Making the diagram

1. Pick the diagram type (below) and start the source with its keyword, such as `flowchart TD`.
2. Keep it small: a diagram a reader can take in at a glance. Split a big system into several diagrams.
3. Keep labels short. Quote any label with punctuation: `A["Sign in (SSO)"]`.
4. Call `create_diagram` with `title`, `source` and, when it helps, `caption`.
5. If SPARK refuses the source, read the message and fix it. A parse error in the page shows as an error in the card, so double-check the syntax rules in `references/syntax-mistakes.md` before you send.
6. Say in a sentence what the diagram shows.

## The diagram types

| Type | Starts with | Use it for |
|---|---|---|
| Flowchart | `flowchart` (`TD` top down, `LR` left to right) | A process, a decision, a pipeline |
| Sequence | `sequenceDiagram` | Who sends what to whom, in order |
| Class | `classDiagram` | Types, fields and relationships in code |
| State | `stateDiagram-v2` | The states of a thing and how it moves between them |
| Entity relationship | `erDiagram` | Tables or entities and how they relate |
| Gantt | `gantt` | A schedule of tasks |
| Timeline | `timeline` | Events across periods |
| Mind map | `mindmap` | A topic broken into branches |
| User journey | `journey` | A person's steps with how they felt |
| Pie | `pie` | A few parts of a whole (for data in a file, use a chart) |
| Git graph | `gitGraph` | Branches and merges |
| Quadrant | `quadrantChart` | Items placed on two axes |

Each has a worked example in `references/diagram-types.md`. Other Mermaid types exist; use one only when you know its syntax well.

## What SPARK refuses

- The source may not change Mermaid's settings: no `%%{init: ...}%%` directive and no front matter `config` block. SPARK draws every diagram at Mermaid's strict security level with HTML labels off, so HTML in a label is shown as text. A `title` in front matter is fine.
- The source must be text and at most 20,000 characters. Past that, simplify or split the diagram.

## Titles and captions

- `title` is required: short and specific, such as "Release process", with no file extension, and never "Untitled" or "Diagram". It names the artefact in the conversation and heads the saved image.
- `caption` is optional: one or two sentences under the diagram.

## References

- `references/diagram-types.md`: an example of each type.
- `references/syntax-mistakes.md`: the mistakes that most often break a diagram, and the fix.
