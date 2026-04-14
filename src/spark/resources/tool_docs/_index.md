# Spark Embedded Tools Documentation Index

This directory contains comprehensive documentation for all embedded tools available in Spark.

## Tool Categories

### Core Tools
| Tool | Description |
|------|-------------|
| `get_current_datetime` | Get current date/time in various formats and timezones |
| `get_tool_documentation` | Retrieve detailed documentation for any tool |

### File System - Reading
| Tool | Description |
|------|-------------|
| `read_file` | Read text file contents with optional line limit |
| `list_directory` | List files and subdirectories |
| `search_files` | Search for files matching glob patterns |
| `get_file_info` | Get file metadata (size, type, dates) |
| `find_in_file` | Search for text patterns within a file |
| `get_directory_tree` | Get directory structure as visual tree |

### File System - Writing
| Tool | Description |
|------|-------------|
| `write_file` | Write content to a file (when write mode enabled) |

### Office Documents — Reading
| Tool | Description |
|------|-------------|
| `read_word` | Extract text from Word documents (.docx) |
| `read_excel` | Read Excel spreadsheet data (.xlsx) |
| `read_pdf` | Extract text from PDF documents |
| `read_powerpoint` | Extract content from PowerPoint presentations (.pptx) |

### Office Documents — Creating (when read_write mode enabled)
| Tool | Description |
|------|-------------|
| `create_word` | Create Word documents with headings, tables, lists, images, formatting |
| `create_excel` | Create Excel workbooks with multiple sheets, headers, formulas, formatting |
| `create_powerpoint` | Create PowerPoint presentations with multiple slide layouts |
| `create_pdf` | Create PDF documents with headings, tables, lists, images, page layout |

### Archives
| Tool | Description |
|------|-------------|
| `list_archive` | List contents of ZIP/TAR archives |
| `extract_archive` | Extract archive contents (when extract mode enabled) |

### System Commands (when enabled)
| Tool | Description |
|------|-------------|
| `run_command` | Execute shell commands, CLI tools, and scripts on the host system |

### Web Search & Retrieval (when enabled)
| Tool | Description |
|------|-------------|
| `web_search` | Search the web using configured engine (DuckDuckGo, Brave, Google, Bing, SearXNG) |
| `web_fetch` | Fetch a URL and return clean, readable text content |

### Memory (always available)
| Tool | Description |
|------|-------------|
| `store_memory` | Store information for future conversations |
| `query_memory` | Search stored memories using semantic search |
| `list_memories` | List all stored memories with filtering |
| `delete_memory` | Remove outdated memories |

## Working with Large Files

When working with large files or datasets:

1. **Inspect before loading**: Use `get_file_info` to check file size first
2. **Use line limits**: `read_file` supports a `max_lines` parameter
3. **Search first**: Use `find_in_file` to locate relevant content before reading entire files
4. **Use tree view**: `get_directory_tree` gives a quick overview of project structure

## Documentation Format

Each tool documentation file follows a standard format:
- **Purpose**: What the tool does
- **Parameters**: Full parameter reference with types and defaults
- **Return Value**: Structure of the response
- **Handling Large Data**: Tool-specific guidance for large files
- **Examples**: Basic and advanced usage examples
- **Best Practices**: Recommended approaches
- **Common Pitfalls**: Issues to avoid
- **Related Tools**: Other tools that work well together
