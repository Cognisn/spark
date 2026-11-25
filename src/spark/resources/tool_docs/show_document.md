# show_document

Show a document to the user in the preview pane beside the conversation.

## Purpose

Presents a document for the user to look at, rather than reading it as text. The chat page opens it in a pane on the right of the conversation, with a card in the message that reopens it, and every member of a shared conversation can open it. PDF, Word (.docx), Excel (.xlsx, .xls) and CSV, images (PNG, JPEG, GIF, WebP, BMP and SVG), Markdown, and text and code files are shown; any other file is offered for download with its name, type and size.

The document comes from one of two places:

- **A link** that one of the user's MCP servers returned, such as a document link from a knowledge base. Spark's server fetches it, only from the host of an MCP server the user is connected to, within the attachment size limit and a time limit, and never follows a redirect to another host. Its content must be what its declared type says. Links to any other host are refused.
- **A file** by id: a file in this conversation (an attachment, a document created in it, or one shown before), or one of the user's own files from `list_my_files`, which is copied into the conversation so its members can open it.

The document is kept with the conversation, in the user's file area, and counts towards their storage quota.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| url | string | One of url or file_id | A document link returned by one of the user's MCP servers |
| file_id | integer | One of url or file_id | A file in this conversation, or one of the user's own files |
| title | string | No | A short title for the preview tab. Default: the file name |

## Return Value

A sentence naming the document, its type, its size and the file id it is kept as, then a line beginning `[spark-preview]` that the chat page draws as the document's card. When the document cannot be shown, the reason, plainly (for example that the link's host is not one of the user's MCP servers, or that the document is larger than the attachment limit).

## Examples

### Show a document link from an MCP server
```json
{"url": "https://kvasir.example.org/document-links/v1/abc123", "title": "Quarterly report"}
```

### Show a file attached in the conversation
```json
{"file_id": 12}
```

## Best Practices

- Use it when the user wants to see a document, not when you need its text: read text with `read_my_file` or the MCP server's own tools.
- Pass a link exactly as the MCP server returned it. Do not build links yourself or change their host.
- A refusal is final for that link: tell the user why rather than trying other addresses.

## Related Tools

- `list_my_files` - Find a file's id
- `read_my_file` - Read a file's text
