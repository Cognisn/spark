# delete_memory

Delete a memory that is no longer relevant or accurate.

## Purpose

The `delete_memory` tool removes outdated or incorrect memories. Use this to clean up information that is no longer valid or has been superseded.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| memory_id | integer | Yes | ID of the memory to delete (from query_memory or list_memories) |

## Return Value

Returns a dictionary with:
- `success`: Boolean indicating if the deletion succeeded
- `memory_id`: ID of the deleted memory
- `message`: Success or error message

## Examples

### Delete a specific memory
```json
{
    "memory_id": 42
}
```

## Best Practices

1. **Verify before deleting** - Use query_memory or list_memories to confirm the content
2. **Delete outdated info** - When facts change, delete old memories
3. **Clean up duplicates** - Remove redundant memories
4. **Consider updating** - Sometimes updating is better than deleting

## When to Delete

- Information is no longer accurate (e.g., user changed jobs)
- Memory is duplicate of another
- User explicitly asks to forget something
- Information was stored incorrectly

## Common Pitfalls

- Deleting wrong memory (always verify the ID)
- Deleting memories that might still be useful
- Not replacing deleted information when needed

## Related Tools

- `query_memory` - Find memories to delete
- `list_memories` - Browse all memories
- `update_memory` - Modify instead of delete
- `store_memory` - Replace with new information
