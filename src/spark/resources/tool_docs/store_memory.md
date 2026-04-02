# store_memory

Store important information to memory for future conversations.

## Purpose

The `store_memory` tool allows you to persist information about the user across conversations. Use this to remember user preferences, facts, project details, instructions, or information about people and organisations.

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| content | string | Yes | The information to remember. Keep it concise and focused on a single fact or preference. |
| category | string | Yes | Category for the memory. Must be one of: `preferences`, `facts`, `projects`, `instructions`, `relationships` |
| importance | number | No | Importance score from 0.0 to 1.0. Default is 0.5. Higher scores make memories more likely to be retrieved. |

## Categories

| Category | Use For | Examples |
|----------|---------|----------|
| preferences | User preferences and style | "Prefers concise responses", "Uses Australian English" |
| facts | Information about the user | "Works at Acme Corp as a developer", "Lives in Sydney" |
| projects | Technical project details | "dtSpark uses FastAPI and SQLite", "Main API is REST-based" |
| instructions | Standing instructions | "Always explain reasoning", "Respond formally" |
| relationships | People and organisations | "Sarah is the team lead", "Acme Corp is the client" |

## Return Value

Returns a dictionary with:
- `success`: Boolean indicating if the memory was stored
- `memory_id`: ID of the created memory
- `content`: The stored content
- `category`: The assigned category
- `importance`: The importance score
- `message`: Success message

## Examples

### Store a user preference
```json
{
    "content": "User prefers responses in British English spelling",
    "category": "preferences",
    "importance": 0.8
}
```

### Store a fact about the user
```json
{
    "content": "User works as a senior software engineer at TechCorp",
    "category": "facts",
    "importance": 0.6
}
```

### Store project information
```json
{
    "content": "The project uses Python 3.11 with FastAPI for the backend",
    "category": "projects",
    "importance": 0.7
}
```

## Best Practices

1. **Keep memories concise** - Focus on a single fact or preference per memory
2. **Choose the right category** - This helps with organisation and retrieval
3. **Set appropriate importance** - Use 0.7+ for critical preferences, 0.5 for general facts
4. **Avoid duplicates** - Check existing memories before storing similar information
5. **Don't store sensitive data** - Never store passwords, API keys, or credentials

## Common Pitfalls

- Storing overly long or complex information (split into multiple memories)
- Using wrong category (affects search relevance)
- Setting all memories to high importance (reduces effectiveness)
- Storing duplicate information

## Related Tools

- `query_memory` - Search stored memories
- `update_memory` - Modify existing memories
- `delete_memory` - Remove outdated memories
- `list_memories` - View all stored memories
