# Tool: get_directory_tree

## Purpose

Generates a visual tree representation of a directory structure. Useful for understanding project layouts, documenting folder structures, or exploring unfamiliar codebases.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| directory | string | Yes | - | Root directory for the tree |
| max_depth | integer | No | 3 | Maximum depth to traverse |
| include_files | boolean | No | true | Include files in the tree |
| include_hidden | boolean | No | false | Include hidden files/directories |
| max_items | integer | No | 500 | Maximum total items to include |

## Return Value

```json
{
    "directory": "/project",
    "tree": "project/\n├── src/\n│   ├── main.py\n│   ├── utils/\n│   │   ├── helpers.py\n│   │   └── config.py\n│   └── tests/\n│       └── test_main.py\n├── docs/\n│   └── README.md\n└── requirements.txt",
    "stats": {
        "directories": 4,
        "files": 6,
        "total_items": 10
    },
    "truncated": false
}
```

Visual representation:
```
project/
├── src/
│   ├── main.py
│   ├── utils/
│   │   ├── helpers.py
│   │   └── config.py
│   └── tests/
│       └── test_main.py
├── docs/
│   └── README.md
└── requirements.txt
```

## Handling Large Directories

For large projects:

1. **Limit depth**: Start with `max_depth: 2` for overview
2. **Exclude files**: Use `include_files: false` for structure only
3. **Target subdirectories**: Get tree of specific folders
4. **Increase gradually**: Expand depth as needed

## Examples

### Basic Tree

```json
{
    "tool": "get_directory_tree",
    "input": {
        "directory": "/project"
    }
}
```

### Directory Structure Only

```json
{
    "tool": "get_directory_tree",
    "input": {
        "directory": "/project",
        "include_files": false,
        "max_depth": 5
    }
}
```

### Shallow Overview

```json
{
    "tool": "get_directory_tree",
    "input": {
        "directory": "/project",
        "max_depth": 1
    }
}
```

### Include Hidden Files

```json
{
    "tool": "get_directory_tree",
    "input": {
        "directory": "/project",
        "include_hidden": true
    }
}
```

## Best Practices

- Start with shallow depth for large projects
- Use `include_files: false` to understand folder organisation first
- Target specific subdirectories for detailed view
- Check `truncated` flag for incomplete trees

## Common Pitfalls

- Deep trees with many files can be overwhelming
- `node_modules`, `.git`, and similar folders create huge trees
- Hidden directories may contain many items

## Related Tools

- `list_files_recursive` - Get file list with metadata
- `search_files` - Find specific files by pattern
- `get_file_stats` - Get info about specific items
