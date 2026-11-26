# The `line` chart

**Use it to:** Show change over ordered categories, usually time: monthly sign-ups, response time by day.

## Encoding

`x` the ordered category such as a date or month (required), `y` the value (required; several columns give one line each), `series` a column that splits the rows into lines (optional). Give several `y` columns or a `series` column, not both.

## Options

`stacked` stacks the lines. `x_title` and `y_title` name the axes.

## Notes

Put the rows in time order. Use one line per series and no more than eight lines.

## Examples

### One line per column

```json
{
  "title": "Monthly sign-ups, 2026",
  "definition": {
    "type": "line",
    "data": {
      "columns": [
        "Month",
        "Web",
        "Mobile"
      ],
      "rows": [
        [
          "Jan",
          120,
          80
        ],
        [
          "Feb",
          135,
          95
        ],
        [
          "Mar",
          150,
          130
        ]
      ]
    },
    "encoding": {
      "x": "Month",
      "y": [
        "Web",
        "Mobile"
      ]
    },
    "y_title": "Sign-ups"
  },
  "caption": "Mobile sign-ups grew fastest."
}
```

### One line per value of a series column

```json
{
  "title": "Response time by region",
  "definition": {
    "type": "line",
    "data": {
      "columns": [
        "Day",
        "Region",
        "Milliseconds"
      ],
      "rows": [
        [
          "Mon",
          "East",
          210
        ],
        [
          "Tue",
          "East",
          190
        ],
        [
          "Mon",
          "West",
          250
        ],
        [
          "Tue",
          "West",
          240
        ]
      ]
    },
    "encoding": {
      "x": "Day",
      "y": "Milliseconds",
      "series": "Region"
    }
  }
}
```
