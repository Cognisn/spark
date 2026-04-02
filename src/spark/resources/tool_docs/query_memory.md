# query_memory

Search stored memories for relevant information using semantic search.

## Purpose

The `query_memory` tool performs semantic search across stored memories to find relevant information. Use this to recall facts, preferences, or context from previous conversations.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| query | string | Yes | Search query describing what to find in memories |
| categories | array[string] | No | Optional filter for categories to search |
| top_k | integer | No | Maximum results to return. Default: 5, Range: 1-20 |

## Return Value

Returns a dictionary with:
- `success`: Boolean indicating if the search completed
- `query`: The original search query
- `result_count`: Number of matches found
- `memories`: Array of matching memories, each containing:
  - `id`: Memory ID (for updates/deletes)
  - `content`: The stored information
  - `category`: Memory category
  - `importance`: Importance score
  - `similarity`: How closely it matches the query (0.0-1.0)
  - `created_at`: When the memory was stored

## Examples

### Search for user preferences
```json
{
    "query": "communication style preferences"
}
```

### Search with category filter
```json
{
    "query": "project technology stack",
    "categories": ["projects"],
    "top_k": 3
}
```

### Search for relationship information
```json
{
    "query": "team members and their roles",
    "categories": ["relationships"]
}
```

## Best Practices

1. **Use specific queries** - "user's name", "user's location", "user's wife" work better than "everything about the user"
2. **Make multiple queries** - For broad requests, make several specific queries rather than one vague query
3. **Use category filters** - When you know what type of information you need
4. **Check similarity scores** - Higher scores (0.4+) indicate relevant matches; below 0.3 may be noise
5. **Use list_memories for "what do you know about me"** - Broad retrieval requests should use `list_memories` instead

## Query Examples

**Good queries (specific):**
- "user's name"
- "user's workplace"
- "user's programming language preferences"
- "project database technology"

**Poor queries (too broad):**
- "user facts" - too vague
- "everything" - won't match specific memories
- "what I know" - not semantically meaningful

## Common Pitfalls

- Using broad queries like "all user information" instead of `list_memories`
- Queries that are too short or vague to match semantically
- Not checking if results are actually relevant (low similarity scores)
- Making one broad query instead of multiple specific ones

## Related Tools

- `store_memory` - Add new memories
- `update_memory` - Modify existing memories
- `delete_memory` - Remove outdated memories
- `list_memories` - View all stored memories
