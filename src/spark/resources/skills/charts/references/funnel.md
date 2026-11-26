# The `funnel` chart

**Use it to:** Show stages that narrow, largest first: visits, sign-ups, purchases.

## Encoding

`x` the stage (required), `y` the value (required; zero or more).

## Options

No options, and no axis titles.

## Notes

Give the stages in order, from the widest to the narrowest.

## Examples

### Stages

```json
{
  "title": "Sign-up funnel",
  "definition": {
    "type": "funnel",
    "data": {
      "columns": [
        "Stage",
        "People"
      ],
      "rows": [
        [
          "Visited",
          1000
        ],
        [
          "Registered",
          400
        ],
        [
          "Subscribed",
          120
        ]
      ]
    },
    "encoding": {
      "x": "Stage",
      "y": "People"
    }
  }
}
```
