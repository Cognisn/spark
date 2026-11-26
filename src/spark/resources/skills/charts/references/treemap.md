# The `treemap` chart

**Use it to:** Show how a total divides into parts, and parts into sub-parts, as nested rectangles sized by value: disk use by folder, budget by department.

## Encoding

`x` the rectangle label (required), `y` the rectangle size (required; more than zero), `series` the parent group of each rectangle (optional).

## Options

No options, and no axis titles.

## Notes

Every size must be more than zero. Use at most 200 rectangles.

## Examples

### Rectangles grouped by a parent

```json
{
  "title": "Budget by department",
  "definition": {
    "type": "treemap",
    "data": {
      "columns": [
        "Item",
        "Cost",
        "Department"
      ],
      "rows": [
        [
          "Laptops",
          40,
          "IT"
        ],
        [
          "Licences",
          25,
          "IT"
        ],
        [
          "Travel",
          30,
          "Sales"
        ],
        [
          "Events",
          20,
          "Sales"
        ]
      ]
    },
    "encoding": {
      "x": "Item",
      "y": "Cost",
      "series": "Department"
    }
  }
}
```
