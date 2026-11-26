# The `area` chart

**Use it to:** Show change over time with the volume filled in, or with `stacked` how parts add up to a total.

## Encoding

`x` the ordered category (required), `y` the value (required; several columns give one area each), `series` a column that splits the rows into areas (optional). Give several `y` columns or a `series` column, not both.

## Options

`stacked` stacks the areas so they show parts of a total. `x_title` and `y_title` name the axes.

## Notes

Stack only values that add up to something meaningful. For comparing separate values, a line reads more clearly.

## Examples

### Stacked areas

```json
{
  "title": "Storage used by kind",
  "definition": {
    "type": "area",
    "data": {
      "columns": [
        "Month",
        "Documents",
        "Images"
      ],
      "rows": [
        [
          "Jan",
          20,
          35
        ],
        [
          "Feb",
          24,
          41
        ],
        [
          "Mar",
          27,
          52
        ]
      ]
    },
    "encoding": {
      "x": "Month",
      "y": [
        "Documents",
        "Images"
      ]
    },
    "y_title": "Gigabytes",
    "stacked": true
  }
}
```
