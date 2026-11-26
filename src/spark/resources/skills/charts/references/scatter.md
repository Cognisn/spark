# The `scatter` chart

**Use it to:** Show the relationship between two numbers, one point per row, and spot clusters and outliers.

## Encoding

`x` a number (required), `y` a number (required), `series` a column whose values group the points (optional), `size` a number that sizes each point, giving a third measure (optional).

## Options

No options. `x_title` and `y_title` name the axes.

## Notes

Both axes are numbers, so do not use a scatter chart for categories. Name the axes with their units.

## Examples

### Points grouped by a series, sized by a third number

```json
{
  "title": "Price against rating",
  "definition": {
    "type": "scatter",
    "data": {
      "columns": [
        "Price",
        "Rating",
        "Kind",
        "Reviews"
      ],
      "rows": [
        [
          10,
          3.8,
          "Basic",
          120
        ],
        [
          25,
          4.2,
          "Basic",
          300
        ],
        [
          60,
          4.6,
          "Premium",
          80
        ],
        [
          85,
          4.8,
          "Premium",
          45
        ]
      ]
    },
    "encoding": {
      "x": "Price",
      "y": "Rating",
      "series": "Kind",
      "size": "Reviews"
    },
    "x_title": "Price (dollars)",
    "y_title": "Rating (out of 5)"
  }
}
```
