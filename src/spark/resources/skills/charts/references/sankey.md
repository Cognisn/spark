# The `sankey` chart

**Use it to:** Show how a quantity flows from sources to targets through stages: visitors to sign-ups, budget to spending.

## Encoding

`x` the source node (required), `y` the target node (required), `size` the flow value (required; more than zero).

## Options

No options, and no axis titles.

## Notes

A Sankey chart cannot have a cycle (flows that lead back to where they started): use `graph` for a network with loops. Network and Sankey charts with more than 40 nodes show labels on hover.

## Examples

### A flow

```json
{
  "title": "Where visitors go",
  "definition": {
    "type": "sankey",
    "data": {
      "columns": [
        "From",
        "To",
        "People"
      ],
      "rows": [
        [
          "Search",
          "Visit",
          300
        ],
        [
          "Visit",
          "Sign up",
          90
        ],
        [
          "Visit",
          "Leave",
          210
        ]
      ]
    },
    "encoding": {
      "x": "From",
      "y": "To",
      "size": "People"
    }
  }
}
```
