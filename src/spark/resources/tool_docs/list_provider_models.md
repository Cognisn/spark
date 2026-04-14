# Tool: list_provider_models

## Purpose

List all available models from the conversation's current LLM provider. Use this to discover which models are available for agent model selection or to check model capabilities.

## Parameters

This tool takes no parameters.

## Return Value

A formatted list of models with their key attributes:

```
Available models from anthropic:

  - claude-sonnet-4-20250514 (Claude Sonnet 4, 200K ctx, tools: yes)
  - claude-haiku-35-20250414 (Claude 3.5 Haiku, 200K ctx, tools: yes)
  - claude-opus-4-20250514 (Claude Opus 4, 200K ctx, tools: yes)
```

Each entry includes:
- **Model ID** — The identifier to pass to `spawn_agent`
- **Name** — Human-readable model name
- **Context window** — Maximum context length
- **Tool support** — Whether the model supports tool use

## Use Case

The primary use case is choosing the right model when spawning agents with `auto_select` model selection enabled:

1. Call `list_provider_models` to see what is available
2. Choose a model that matches the task complexity
3. Pass the `model_id` to `spawn_agent`

## Examples

### List Available Models

```json
{
    "tool": "list_provider_models",
    "input": {}
}
```

## Related Tools

- `spawn_agent` — Spawn a sub-agent, optionally specifying a model ID
