# Common syntax mistakes

Mermaid is strict, and a single slip stops the whole diagram drawing. Check these before you call `create_diagram`.

| Mistake | Fix |
|---|---|
| Punctuation in an unquoted label: `A[Sign in (SSO)]` | Quote it: `A["Sign in (SSO)"]` |
| The word `end` in lower case as a node label | Capitalise it (`End`) or quote it: `A["end"]` |
| A node id that starts with a digit or holds spaces | Use letters and digits with no spaces: `step1`, and put the words in the label: `step1[Step one]` |
| `graph` and `flowchart` mixed up | Start with `flowchart TD` or `flowchart LR` |
| No diagram keyword on the first line | The first line is the type: `flowchart TD`, `sequenceDiagram`, and so on |
| `<br>` or other HTML in a label, expecting it to render | HTML labels are off. Use a short label, or `<br/>` only where the type documents it, and expect plain text otherwise |
| A tab or inconsistent indent in `mindmap` | Indent branches with spaces, two per level, and keep to one root |
| `;` or `#` inside a label | Quote the label, or use words instead |
| An arrow with the wrong form for the type | Flowchart `-->`, `---`, `-.->`; sequence `->>` and `-->>`; class `<\|--`; state `-->` |
| A `%%{init}%%` directive or `config` block | Refused. Remove it: SPARK sets how diagrams are drawn |
| A subgraph or `alt` or `loop` with no closing `end` | Close every block |
| Duplicate participant, class or node names with different meanings | Give each a unique id and put the readable name in the label |
| Very long labels or more than about 30 nodes | Shorten the labels and split the diagram |
| A Gantt date that does not match `dateFormat` | Use the format you declared: `dateFormat YYYY-MM-DD` and dates such as `2026-03-01` |
| A comment on the same line as a statement | Put `%% comment` on a line of its own (a `%%` line is fine, but `%%{` is the refused directive) |

## When SPARK or the page reports an error

- A refusal from `create_diagram` is explained in its answer: fix the source and call again.
- A diagram that is kept but fails to draw shows the error in its card. Simplify the diagram, check the table above, and make a new diagram with the same title.
- Do not claim the diagram worked until the tool answered that it is drawn.
