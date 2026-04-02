# list_memories

List all stored memories with optional filtering.

## Purpose

The `list_memories` tool retrieves all stored memories for the current user. Use this to browse what has been remembered, find specific memories to update or delete, or get an overview of stored information.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| category | string | No | Filter by specific category (preferences, facts, projects, instructions, relationships) |
| limit | integer | No | Maximum memories to return. Default: 20, Range: 1-100 |

## Return Value

Returns a dictionary with:
- `success`: Boolean indicating success
- `total_memories`: Total count of all memories for the user
- `by_category`: Count breakdown by category
- `returned_count`: Number of memories in this response
- `memories`: Array of memories, each containing:
  - `id`: Memory ID
  - `content`: The stored information
  - `category`: Memory category
  - `importance`: Importance score (0.0-1.0)
  - `created_at`: When stored
  - `last_accessed`: When last retrieved

## Examples

### List all memories
```json
{}
```

### List preferences only
```json
{
    "category": "preferences",
    "limit": 10
}
```

### Get project memories
```json
{
    "category": "projects",
    "limit": 50
}
```

## Best Practices

1. **Use category filter** - When you need specific types of memories
2. **Check totals** - Use the stats to understand memory distribution
3. **Find IDs for updates** - Use list_memories to get IDs before updating/deleting
4. **Review periodically** - Check for outdated memories to clean up

## Common Pitfalls

- Requesting too many memories at once
- Not using category filter when appropriate
- Forgetting that results are sorted by importance

## Related Tools

- `query_memory` - Semantic search across memories
- `store_memory` - Add new memories
- `update_memory` - Modify existing memories
- `delete_memory` - Remove memories
