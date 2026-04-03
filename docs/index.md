# Spark Documentation

Spark is a secure personal AI research kit -- a multi-provider LLM web interface with integrated tool support, persistent memory, and autonomous action scheduling.

**Package:** `cognisn-spark` (PyPI) / `spark` (import)
**Python:** 3.12+
**License:** MIT with Commons Clause

## Documentation

### Setup

- [Getting Started](getting-started.md) -- Installation, first run, initial configuration
- [Configuration](configuration.md) -- Complete config.yaml reference, environment variables, secrets
- [Providers](providers.md) -- Setting up each LLM provider (Anthropic, AWS Bedrock, Ollama, Google Gemini, X.AI)

### Features

- [Conversations](conversations.md) -- Chat interface, message handling, context compaction, linking, export
- [Tools](tools.md) -- Built-in tools (filesystem, documents, archives, web, memory, datetime), permissions
- [MCP Integration](mcp-integration.md) -- Setting up MCP servers, tool management, per-conversation control
- [Memory](memory.md) -- Persistent memory system, categories, semantic search, import/export
- [Autonomous Actions](autonomous-actions.md) -- Scheduled AI tasks, cron/one-off, daemon/tray
- [Voice](voice.md) -- Speech-to-text input, voice conversation mode, TTS voice selection
- [Web Search](web-search.md) -- Search engine options and configuration

### Security and Maintenance

- [Security](security.md) -- Prompt inspection, tool permissions, settings lock, secret management
- [Auto-Update](auto-update.md) -- How updates work, PyApp self-update, pip upgrade

### Technical Reference

- [Architecture](architecture.md) -- System architecture, component overview, data flow, database schema
- [API Reference](api-reference.md) -- Web API endpoints reference
- [Development](development.md) -- Contributing, project structure, testing, building, releasing

## Quick Start

```bash
pip install cognisn-spark
spark
```

On first launch Spark creates a default `config.yaml`, starts a local web server, and opens your browser. Navigate to **Settings** to enable at least one LLM provider, then create your first conversation.

## Links

- [GitHub Repository](https://github.com/Cognisn/spark)
- [PyPI Package](https://pypi.org/project/cognisn-spark/)
- [Issue Tracker](https://github.com/Cognisn/spark/issues)
- [Changelog](https://github.com/Cognisn/spark/blob/main/CHANGELOG.md)
