# The `pie` chart

**Use it to:** Show the parts of one whole when there are only a few of them (eight or fewer) and the parts add up to something meaningful.

## Encoding

`x` the slice label (required), `y` the slice value (required; zero or more).

## Options

`donut` draws the pie as a ring. Axis titles do not apply to a pie chart.

## Notes

If there are more than a handful of parts, or the differences are small, use a bar chart instead. Group the small parts as "Other".

## Examples

### A ring

```json
{
  "title": "Support tickets by channel",
  "definition": {
    "type": "pie",
    "data": {
      "columns": [
        "Channel",
        "Tickets"
      ],
      "rows": [
        [
          "Email",
          54
        ],
        [
          "Chat",
          31
        ],
        [
          "Phone",
          15
        ]
      ]
    },
    "encoding": {
      "x": "Channel",
      "y": "Tickets"
    },
    "donut": true
  }
}
```
