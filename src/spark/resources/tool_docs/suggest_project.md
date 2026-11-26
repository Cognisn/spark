# suggest_project

Suggest a new project to the user, who decides.

## Purpose

Shows the user a card proposing a project, with Create, Edit and Dismiss. Nothing is created unless the user accepts: Create makes it as proposed, Edit lets them change the name, description and instructions first. Use it when you judge a project would help and the user has not asked for one: instructions they keep repeating, the same files across conversations, one subject spread over several chats.

Spark allows one suggestion per conversation. After a suggestion has been shown, whatever the user did with it, and in particular after Dismiss, Spark refuses another in the same conversation. Do not press the point.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| name | string | Yes | A short, specific project name |
| description | string | No | One or two sentences on what the project is for |
| instructions | string | No | Instructions every conversation in the project would receive |
| reason | string | No | One sentence for the user on why a project would help (shown on the card) |

## Return Value

A line beginning `[spark-project-card]` that the chat page draws as the card, then a sentence saying the card is shown and nothing is created unless the user accepts. When no card is shown, the reason (for example that this conversation has already had its suggestion).

## Examples

```json
{"name": "Garden", "description": "Planning the vegetable garden.", "instructions": "Use metric units.", "reason": "You have asked about the garden in several conversations."}
```

## Best Practices

- Suggest once, at a natural pause, with a clear reason.
- If the user asks for a project in their own words, use `create_project` instead.

## Common Pitfalls

- Calling `create_project` after a suggestion the user has not accepted: the user did not ask, so Spark refuses.

## Related Tools

- `create_project` - Create a project the user asked for
