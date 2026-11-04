# list_my_files

List the files in the user's own file area.

## Purpose

The user's file area holds the files they attach in chat and the documents Spark creates for them. `list_my_files` names each one with its file id, which `read_my_file` takes. It lists the signed-in user's files only.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| conversation_only | boolean | No | Only files attached to or created in this conversation. Default: false |

## Return Value

One line per file, newest first: `- file <id>: <name> (<type>, <size> bytes), added <time>`, with `[this conversation]` after files belonging to the current conversation. "You have no files." when the area is empty.

## Examples

### Every file
```json
{}
```

### This conversation's files
```json
{"conversation_only": true}
```

## Best Practices

- Files the user attached to the current message are already named in it, with their ids, after the line `[Attached files: read them with the read_my_file tool]`; there is no need to list them first.
- Use the list when the user refers to a file from an earlier conversation.

## Related Tools

- `read_my_file` - Read one of the files by id
