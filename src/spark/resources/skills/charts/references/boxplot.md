# The `boxplot` chart

**Use it to:** Show the spread of values per group (quartiles, whiskers and outliers) from the raw values: response times by server, scores by class.

## Encoding

`x` the group (required), `y` the raw values to summarise (required). Give one row per observation, not summaries.

## Options

No options. `x_title` and `y_title` name the axes.

## Notes

SPARK works out the quartiles itself, so pass the raw values, not precomputed ones.

## Examples

### Raw values per group

```json
{
  "title": "Response time by server",
  "definition": {
    "type": "boxplot",
    "data": {
      "columns": [
        "Server",
        "Milliseconds"
      ],
      "rows": [
        [
          "A",
          110
        ],
        [
          "A",
          125
        ],
        [
          "A",
          130
        ],
        [
          "A",
          160
        ],
        [
          "B",
          90
        ],
        [
          "B",
          95
        ],
        [
          "B",
          105
        ],
        [
          "B",
          240
        ]
      ]
    },
    "encoding": {
      "x": "Server",
      "y": "Milliseconds"
    },
    "y_title": "Milliseconds"
  }
}
```
