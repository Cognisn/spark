# User Bundles

A user bundle moves one person's Spark from a personal installation onto a Spark
server. The person exports a bundle in personal mode; a server administrator imports
it for that person's account. A bundle carries the user's own data and nothing of the
instance it came from: no secret, no provider, no MCP server configuration and no
other instance setting ever leaves or enters through one.

## Exporting (personal mode)

- **In the web interface:** Settings, **Export my data**. The browser downloads
  `spark-bundle-<date>-<id>.zip`.
- **From the command line:** `spark export [--output PATH]`. Without `--output` the
  bundle is written to the working directory under the same kind of name. An existing
  file is never overwritten. The command prints what the bundle holds and what was
  left out.

A server has no export: its users are moved by an administrator, server to server
migration is not offered.

### What a bundle holds

| Section | Tables |
|---|---|
| Conversations | `conversations`, `messages`, `rollup_history`, `conversation_files` (0.2 attachments), `conversation_links`, `conversation_model_usage`, `conversation_tool_permissions`, `conversation_embedded_tools`, `conversation_mcp_servers`, `conversation_skills`, `context_index_elements`, `mcp_transactions`, `agent_runs`, `debate_config`, `debate_agents`, `debate_turns`, `debate_exhibits` |
| Files | `user_files` with each file's content |
| Memories | `user_memories` (with their embeddings) |
| Knowledge graphs | `kg_nodes`, `kg_edges`, `kg_builds` |
| Skills | the user's own skill folders, and their enable states (`skills`) |
| "Always" approvals | `global_tool_permissions` |
| Scheduled actions | `autonomous_actions`, `action_tool_permissions`, `action_runs` |
| Preferences | the user-scope settings: default model, global instructions, theme, voice |

### What a bundle never holds

- Secrets. Every text value has each secret the instance holds, and anything shaped
  like a well-known credential (provider and cloud keys, GitHub and Slack tokens,
  Spark API keys, private keys), replaced by `[redacted credential]`. A file or a
  binary skill file that holds one is left out and listed.
- Instance configuration: providers and their models, MCP servers (only the transport
  of each server the data mentions is written, so an import can tell stdio servers
  apart), tool policy, file storage, email and voice settings.
- The instance's own records: usage and voice usage, prompt inspection violations and
  the tray daemon's registry.
- Any other user's data, and the user's own guid.

## The format, version 1

A bundle is one zip archive:

```
manifest.json              what the bundle is, and every member's size and sha256
data/<table>.jsonl         one JSON object per row, for the tables above only
files/<sha256>             a file's content, named by its sha256
skills/<name>/<path>       one file of one of the user's skills
preferences.json           the user's preferences
```

`manifest.json`:

| Field | Meaning |
|---|---|
| `format` | always `spark-user-bundle` |
| `format_version` | `1`; an import refuses any other version |
| `bundle_id` | 32 hexadecimal characters, random, one per export |
| `created_at`, `spark_version`, `source_mode` | when, by which Spark, in which mode (`personal`) |
| `counts` | rows per table |
| `members` | every other member's `size` and `sha256` |
| `mcp_transports` | each MCP server the data mentions: `stdio`, `http`, `sse` or `unknown` |

Rows carry only the columns Spark lists for their table (`spark.bundles.format.TABLES`)
and never a `user_guid`. Ids are the exporting database's; references between rows use
them. A binary value (an embedding) is written as `{"$hex": "..."}`. A `user_files`
row names its content with `blob`, the sha256 of `files/<sha256>`.

## Importing (server mode)

An administrator (the `spark.bundles.import` permission, which only the Administrator
role holds) opens **Import a user bundle** from the Account menu (`/admin/bundles`),
chooses the user, uploads the bundle and sees the import report on screen.

### Checks before anything is written

The upload is cut off once it passes 1 GB. The archive is then refused, and nothing
written, if it:

- is not a zip archive, has more than 200,000 members, unpacks to more than 4 GB, or
  has a member that expands more than 200 times (from 1 MB up);
- has a member named outside the layout above: an absolute path, a `..` component, a
  backslash, a hidden or device name, an unknown table, the same name twice;
- has a link, another non-regular file or an encrypted member;
- has no readable manifest, another format or another format version;
- lacks a member the manifest lists, has one it does not list, or has one whose size
  or sha256 differs (a file's content must also match its name);
- holds anything shaped like a credential, anywhere.

Nothing is extracted to disk: members are read from the archive, and files are written
through the user's file area, which contains every key again.

### What changes on the way in

- Every scheduled action arrives **disabled**, its lock cleared. A name the user
  already uses gets ` (imported)`.
- What a server cannot run is **left out and listed**: "Always" approvals and
  per-conversation settings of the local-only tools (filesystem, system commands,
  email) and of MCP tools, per-conversation MCP servers that are stdio or that this
  server does not offer over HTTP or SSE, and the same in an action's tool
  permissions.
- The user's own data wins: a memory, an approval, a skill state or a preference the
  user already has is kept. Knowledge graph entities and relationships with the same
  name merge, their weights added.
- Files land in the user's area under `imports/<bundle id>/`, counted against the
  user's quota; an import over the quota is refused.
- A server's skills are shared by every user, so imported skills are kept in the
  user's file area under `imports/<bundle id>/.../skills/<name>/` and listed. An
  administrator can install one for everyone.
- On PostgreSQL, imported memory and context vectors are added to their vector index.

### The report, the receipt and importing again

The report lists the rows imported per table, entities merged, actions imported
disabled, skills kept, what was left out and why, what was skipped, and warnings. It
is also kept in the user's files as `imports/<bundle id>/report.json`. That receipt is
written in the same transaction as the data, so **a bundle is imported once per
user**: importing it again for the same user is refused and changes nothing. The same
bundle may be imported for a different user.

### Audit

Each import is recorded in the audit trail as `spark.bundle.imported`, in the same
transaction as the data, with the administrator as actor, the target user as the
resource, and the bundle id, format version and numbers of rows, files and items left
out. A refused import is recorded as a failure with a reason code. No content is ever
recorded.
