# The `bar` chart

**Use it to:** Compare values across categories: sales by region, tickets by team, counts by status.

## Encoding

`x` the category (required), `y` the value (required; several columns give one series each), `series` a column that splits the rows into series (optional). Give several `y` columns or a `series` column, not both.

## Options

`stacked` stacks the series (parts of a total); `horizontal` draws horizontal bars (use it for long category names or many categories). `x_title` and `y_title` name the axes.

## Notes

Sort categories in the order a reader expects (largest first, or time order). Keep to eight series or fewer.

## Examples

### Units sold by quarter, stacked

```json
{
  "title": "Units sold by quarter",
  "definition": {
    "type": "bar",
    "data": {
      "columns": [
        "Quarter",
        "North",
        "South"
      ],
      "rows": [
        [
          "Q1",
          10,
          12
        ],
        [
          "Q2",
          14,
          9
        ],
        [
          "Q3",
          18,
          15
        ]
      ]
    },
    "encoding": {
      "x": "Quarter",
      "y": [
        "North",
        "South"
      ]
    },
    "x_title": "Quarter",
    "y_title": "Units",
    "stacked": true
  },
  "caption": "North overtook South in the third quarter."
}
```

### Horizontal bars with a series column

```json
{
  "title": "Tickets by team and priority",
  "definition": {
    "type": "bar",
    "data": {
      "columns": [
        "Team",
        "Priority",
        "Tickets"
      ],
      "rows": [
        [
          "Support",
          "High",
          12
        ],
        [
          "Support",
          "Low",
          30
        ],
        [
          "Platform",
          "High",
          7
        ],
        [
          "Platform",
          "Low",
          18
        ]
      ]
    },
    "encoding": {
      "x": "Team",
      "y": "Tickets",
      "series": "Priority"
    },
    "horizontal": true
  }
}
```
