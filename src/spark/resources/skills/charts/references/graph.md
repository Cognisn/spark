# The `graph` chart

**Use it to:** Show a network of things and the links between them: services that call each other, who reports to whom. Use it instead of `sankey` when the links can loop.

## Encoding

`x` the source node (required), `y` the target node (required), `size` the link weight (optional; more than zero, 1 per row by default), `series` the group of the source node (optional).

## Options

No options, and no axis titles.

## Notes

Use at most 200 nodes and 1,000 links. Labels show on hover above 40 nodes.

## Examples

### A network with groups

```json
{
  "title": "Service dependencies",
  "definition": {
    "type": "graph",
    "data": {
      "columns": [
        "Caller",
        "Callee",
        "Calls",
        "Layer"
      ],
      "rows": [
        [
          "Web",
          "Api",
          50,
          "Front"
        ],
        [
          "Api",
          "Database",
          40,
          "Back"
        ],
        [
          "Api",
          "Cache",
          30,
          "Back"
        ],
        [
          "Cache",
          "Api",
          5,
          "Back"
        ]
      ]
    },
    "encoding": {
      "x": "Caller",
      "y": "Callee",
      "size": "Calls",
      "series": "Layer"
    }
  }
}
```
