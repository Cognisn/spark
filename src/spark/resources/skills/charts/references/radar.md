# The `radar` chart

**Use it to:** Compare a few things across several measures on spokes: product scores on speed, cost and support. One shape per series.

## Encoding

`x` the measure, one spoke each (required), `y` the value (required; zero or more; several columns give one shape each), `series` a column whose values make the shapes (optional).

## Options

No options, and no axis titles.

## Notes

Use from 3 to 20 measures, on a common scale.

## Examples

### One shape per column

```json
{
  "title": "Plan comparison",
  "definition": {
    "type": "radar",
    "data": {
      "columns": [
        "Measure",
        "Basic",
        "Plus"
      ],
      "rows": [
        [
          "Speed",
          6,
          8
        ],
        [
          "Cost",
          9,
          6
        ],
        [
          "Support",
          5,
          9
        ],
        [
          "Storage",
          4,
          7
        ]
      ]
    },
    "encoding": {
      "x": "Measure",
      "y": [
        "Basic",
        "Plus"
      ]
    }
  }
}
```
