# move_conversation_to_project

Propose moving this conversation into a project, or out of its project.

## Purpose

Shows the user a card saying where the conversation would go and who would gain or lose sight of it, with Confirm and Cancel. Moving always asks first: a project's sharing decides who sees the conversations in it, so a move can show the conversation to more people or hide it from people who see it now, and individual shares with people outside the project end. Nothing moves until the user confirms. If who would be affected changes before they confirm, the card is shown again with the new warning.

The projects rules apply: the user must have started the conversation and own the destination project; a conversation in one project reaches another only through no project. The new project's instructions and resources apply from the next message.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| project_id | integer or null | Yes | The project to move the conversation into, or null to move it out of its project |

## Return Value

A line beginning `[spark-project-card]` that the chat page draws as the card, then a sentence saying how many people the warning names. When no card is shown, the reason (for example that the project is not the user's, or the conversation is already there).

## Examples

```json
{"project_id": 12}
```

## Related Tools

- `create_project` - Create the project first, when the user asked for one
