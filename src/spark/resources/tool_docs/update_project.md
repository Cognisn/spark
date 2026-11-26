# update_project

Propose a change to a project the user owns.

## Purpose

Shows the user a card with each changed field before and after, and any files to add as resources, with Confirm and Cancel. Nothing changes until the user confirms, even when they asked for the change, because a project's instructions and resources reach every conversation in it. Only the project's owner is offered a card: for a project the user only contributes to or reads, or cannot reach, Spark refuses.

The tool can change a project's name, description and instructions, and add the user's own files from this conversation as its resources. It never deletes a project, shares it or changes its members: those are for the user, on the project's page.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| project_id | integer | Yes | The project to change |
| changes | object | Yes | Only the fields to change: `name`, `description`, `instructions` (strings) and `add_file_ids` (a list of file ids of the user's files in this conversation) |

## Return Value

A line beginning `[spark-project-card]` that the chat page draws as the card, then a sentence naming what the card proposes. When no card is shown, the reason: the project is not the user's, nothing would change, a file is not one of the user's files in this conversation or is already a resource, or a field is too long.

## Examples

### New instructions
```json
{"project_id": 12, "changes": {"instructions": "Write dates as 2026-03-01."}}
```

### Add this conversation's files as resources
```json
{"project_id": 12, "changes": {"add_file_ids": [41, 42]}}
```

## Best Practices

- Send the whole new text of a field, not a fragment: the card shows the field before and after.
- Tell the user the change waits for their Confirm.

## Related Tools

- `list_my_files` - Find the ids of the user's files in this conversation
- `create_project` - Create a project the user asked for
