# The `heatmap` chart

**Use it to:** Show a value for every pair of two categories as a coloured grid: activity by weekday and hour, errors by service and day.

## Encoding

`x` the column category (required), `y` the row category (required), `colour` the number that colours each cell (required).

## Options

No options. `x_title` and `y_title` name the axes.

## Notes

Keep to at most 100 categories on each side. Every cell is one row of data.

## Examples

### A grid

```json
{
  "title": "Requests by weekday and period",
  "definition": {
    "type": "heatmap",
    "data": {
      "columns": [
        "Day",
        "Period",
        "Requests"
      ],
      "rows": [
        [
          "Mon",
          "Morning",
          120
        ],
        [
          "Mon",
          "Evening",
          80
        ],
        [
          "Tue",
          "Morning",
          140
        ],
        [
          "Tue",
          "Evening",
          95
        ]
      ]
    },
    "encoding": {
      "x": "Day",
      "y": "Period",
      "colour": "Requests"
    }
  }
}
```
