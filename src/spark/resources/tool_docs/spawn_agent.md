# Tool: spawn_agent

## Purpose

Spawn an independent sub-agent to work on a delegated task. Sub-agents run concurrently alongside the main conversation and have access to all enabled tools. Use this to parallelise research, data gathering, analysis, file processing, and other tasks that can be performed independently.

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| task | string | Yes | - | Clear, detailed description of what the agent should accomplish. Include all necessary context, constraints, and expected output format. |
| agent_name | string | Yes | - | Short identifier for the agent (e.g. "research-agent", "data-gatherer"). Displayed in the Agents tab. |
| model_id | string | No | Same as conversation | Specific model ID to use for this agent. Only available when model selection is set to `auto_select`. Use `list_provider_models` to see available models. |

## Return Value

```json
{
    "agent_id": "a1b2c3d4-...",
    "agent_name": "research-agent",
    "status": "running",
    "message": "Agent 'research-agent' spawned successfully."
}
```

The agent runs asynchronously. Monitor progress in the **Agents tab** of the sidecar panel.

## Modes

Spark supports two agent execution modes, configured in Settings:

### Orchestrator-Workers (default)

Each agent receives a fresh context containing only the task description. The agent does not see the conversation history.

- **Best for:** Independent research, data gathering from multiple sources, parallel file processing
- **Advantage:** Agents are not influenced by prior conversation; each starts with a clean slate
- **Trade-off:** You must include all necessary context in the `task` parameter

### Chain

Agents inherit the full conversation history up to the point of spawning.

- **Best for:** Contextual follow-up tasks, tasks that reference earlier conversation content
- **Advantage:** Agents understand the full context of the discussion
- **Trade-off:** Higher token usage due to the inherited context

## When to Use

- **Research tasks** — "Search for recent developments in quantum computing and summarise the top 5 breakthroughs"
- **Data gathering from multiple sources** — "Read all `.csv` files in `/data/` and produce a summary of each"
- **Parallel analysis** — "Analyse the security posture of these three repositories"
- **File processing** — "Convert all Word documents in `/reports/` to PDF"
- **Report generation** — "Generate a competitive analysis report based on these company websites"

## When NOT to Use

- **Simple questions** — If the answer requires no tools or a single tool call, just answer directly
- **Single tool calls** — No need to spawn an agent just to read one file or run one command
- **Tasks requiring conversation context** — Unless using chain mode, agents do not see prior messages
- **Recursive spawning** — Agents cannot spawn further sub-agents; this is not permitted

## Model Selection

When model selection is set to `auto_select`, you can choose the most appropriate model for each agent:

1. Use `list_provider_models` to see available models from the current provider
2. Consider the task complexity:
   - **Simple data gathering:** Use a faster, cheaper model
   - **Complex analysis or reasoning:** Use a more capable model
3. Pass the chosen `model_id` to `spawn_agent`

When model selection is set to `same`, agents always use the conversation's current model.

**Note:** When you specify a `model_id`, the user is shown a confirmation modal before the agent starts, allowing them to approve or change the model.

## Examples

### Research Agent

```json
{
    "tool": "spawn_agent",
    "input": {
        "task": "Search the web for the latest developments in large language model efficiency techniques published in 2026. Focus on quantisation, distillation, and architecture improvements. Summarise the top 5 findings with source URLs.",
        "agent_name": "llm-research"
    }
}
```

### Data Gathering Agent

```json
{
    "tool": "spawn_agent",
    "input": {
        "task": "Read all JSON files in /Users/matt/data/api-responses/ and produce a summary table showing: filename, response status, record count, and any errors found.",
        "agent_name": "data-summary"
    }
}
```

### Analysis Agent with Model Selection

```json
{
    "tool": "spawn_agent",
    "input": {
        "task": "Analyse the Python codebase in /Users/matt/projects/my-app/src/ for potential security vulnerabilities. Check for SQL injection, path traversal, insecure deserialisation, and hardcoded credentials. Produce a detailed report.",
        "agent_name": "security-audit",
        "model_id": "claude-sonnet-4-20250514"
    }
}
```

## Best Practices

- **Give clear, detailed task descriptions** — The agent only knows what you tell it. Include all relevant context, file paths, constraints, and expected output format.
- **Name agents descriptively** — Use names like "security-audit", "api-research", or "csv-processor" rather than "agent-1".
- **Monitor progress** — Check the Agents tab in the sidecar panel to see agent status, tool calls, and results.
- **Use appropriate models** — When auto-select is enabled, match the model to the task complexity.
- **Spawn multiple agents for parallel work** — If you have several independent tasks, spawn agents for each rather than doing them sequentially.

## Common Pitfalls

- **Vague task descriptions** — "Look into this" gives the agent nothing to work with. Be specific about what to investigate, where to look, and what to produce.
- **Spawning agents for trivial tasks** — A single file read or web search does not warrant an agent. Use the tool directly.
- **Assuming conversation context** — In orchestrator mode, agents do not see any prior messages. Include all necessary information in the task.
- **Recursive spawning** — Agents cannot spawn sub-agents. Design tasks to be self-contained.
- **Too many concurrent agents** — Respect the `max_concurrent` limit (default: 5). Spawning too many agents at once may degrade performance.

## Related Tools

- `list_provider_models` — List available models for agent model selection
- `get_tool_documentation` — Retrieve documentation for any tool the agent might use
