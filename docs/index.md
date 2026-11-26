# Spark Documentation

Spark is a secure personal AI research kit -- a multi-provider LLM web interface with integrated tool support, persistent memory, and autonomous action scheduling.

**Package:** `cognisn-spark` (PyPI) / `spark` (import)
**Python:** 3.12+
**License:** MIT with Commons Clause

## Documentation

### Setup

- [Getting Started](getting-started.md) -- Installation, first run, initial configuration
- [Configuration](configuration.md) -- Complete config.yaml reference, environment variables, secrets
- [Connections](connections.md) -- Adding model connections: each service's fields and authentication, Region and residency, discovery, declaring models, tiers; personal mode; upgrading from 0.2
- [Model Services](providers.md) -- Getting an account and a credential from each service
- [Metering](metering.md) -- Credits, metered connections, assignments and monthly allowances (server mode)
- [Containers](container.md) -- The container image, the web and worker roles, Compose and upgrades
- [Server Deployment](deployment/index.md) -- Running a multi-user Spark server: Compose, secrets, TLS, identity, backups and upgrades
- [User Bundles](bundles.md) -- Exporting a personal user's data and importing it into a server
- [Projects](projects.md) -- Projects and sub-projects: instructions, resources, sharing, moves, deletion and transfer

### Features

- [Conversations](conversations.md) -- Chat interface, message handling, context compaction, linking, export
- [Tools](tools.md) -- Built-in tools (filesystem, documents, archives, web, memory, datetime), permissions
- [Charts and Diagrams](charts.md) -- Charts and Mermaid diagrams the assistant draws, their limits, and saving them
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

On first launch Spark creates a default `config.yaml`, starts a local web server, and opens your browser. Add at least one model connection in **Settings > Connections**, then create your first conversation.

## Links

- [GitHub Repository](https://github.com/Cognisn/spark)
- [PyPI Package](https://pypi.org/project/cognisn-spark/)
- [Issue Tracker](https://github.com/Cognisn/spark/issues)
- [Changelog](https://github.com/Cognisn/spark/blob/main/CHANGELOG.md)
