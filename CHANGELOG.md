# Changelog

All notable changes to Spark will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Usage per model call and monthly token budgets** (both engines): a new revision gives every `usage_tracking` row the source of its model call (chat, compaction, agent, debate, panel or action), the action it ran for and its period, the UTC calendar month it was recorded in; existing rows take the month of their timestamp. Two new tables hold a monthly token budget per role and an optional per-user override, where an override may also be "unlimited". The usage repository records one row per call, sums a user's tokens for a month, reports usage by month, user and model, and sets and clears budgets. Downgrading removes the tables and columns
- **Usage metering and budget policy** (`spark.usage`): a metering wrapper records every model call a turn makes against the turn's user, with the model the call used and its source; input tokens include prompt-cache reads and writes, and a failure to record never breaks the call. The budget policy decides a user's monthly limit (their own override first, then on a server the most generous budget among their roles, otherwise unlimited), warns at 80%, refuses a new request at 100% with a message saying when the budget resets (00:00 UTC on the first of next month), and audits the refusal. Personal mode has no budget unless the local user's own budget is set. A new permission, `spark.usage.admin`, held only by Administrators, covers the usage view and budgets, and three audit actions record budget refusals, usage views and budget changes

### Changed
- **Chat turns are metered and budgeted**: every model call a chat turn makes, its tool loop, its sub-agents and its context compaction (with a locked compaction model too) is recorded against the turn's user. A turn checks the user's monthly token budget before anything is stored: at 100% it is refused with a clear message, while a turn already running always finishes, even past the budget. The turn's result carries the user's budget standing for the UI's warning. The per-conversation token counters are unchanged

## [0.3.0a10] - 2026-11-03

### Added
- **warden's migration chain** (server mode): `spark migrate` runs warden's own migration chain, in its `warden` schema, after SPARK's and prints both revisions. The web process and `spark worker` refuse a database whose warden chain is behind, as they do for SPARK's own (`spark migrate` first). Personal mode is unchanged: SQLite has no warden schema, so the chain is skipped
- **User identities** (`user_identities`, both engines): each SPARK user guid is mapped onto one warden user. Existing data keeps its guid and stays attributable once that guid is linked to a warden user; a new warden user's guid is created on first use, and neither side of a mapping is ever rebound
- **Shared pool for warden**: warden works over SPARK's own PostgreSQL connection pool, and an audit event written inside a SPARK transaction commits or rolls back with it
- **Roles and permissions** (server mode, `spark.identity.permissions`): SPARK declares its permissions to warden, one per tool category (`spark.tools.<category>`) and one per capability (`spark.actions`, `spark.agents`, `spark.knowledge.instance`, `spark.settings.instance`, `spark.users.admin`, `spark.audit.read`, `spark.audit.export`, `spark.api_keys`), and four roles built from them: Administrator (everything), Auditor (reading and exporting the audit trail only), User (the tools a server offers, apart from sub-agents, and their own API keys) and Power user (a User who may also run scheduled actions, spawn sub-agents and build instance-wide knowledge graphs). Roles are data, so a new role needs no code change
- **Sign-in through warden** (server mode): users sign in at `/auth/sign-in` with their email address and password. warden's access and refresh tokens live in HttpOnly, Secure, SameSite=Strict cookies, the refresh token's scoped to the refresh path only, and an expired access token is refreshed on the server: a page load passes through the refresh path and comes back, and a script's request is answered `401 X-Spark-Session: refresh`, which `csrf.js` handles by refreshing once (one refresh at a time, shared across tabs where the browser allows) and retrying. Sign-out revokes the warden session. A second-factor challenge lands on a placeholder screen that a later build completes. Each request's user guid is the signed-in warden user's, so every user sees only their own data, and endpoints never fall back to the process's own guid. Personal mode keeps its terminal sign-in code
- **CSRF bound to the warden session** (server mode): every state-changing request carries a token that is an HMAC of its warden session, under a per-instance key kept in konfig's secrets store; a token from another session never matches. Requests authenticated by an API key (`Authorization: Bearer wdn_...`, limited to the key's scopes) need no token, but are still Origin checked when they come from a browser, and may not be combined with a session cookie
- **`spark identity create-admin`**: creates a server's first administrator through warden (safe to run again), reading the password from standard input or a prompt; `--link-user-guid` binds the interim single-user guid to the administrator so existing data stays theirs
- **warden at start-up** (server mode): the web process starts warden over SPARK's database and konfig's secrets store, rotating in a signing key and generating the TOTP and CSRF keys when they are missing, and installs warden's audit recorder

- **Audit events for SPARK's actions** (server mode): SPARK records its own actions in warden's audit trail beside warden's identity and administration events: instance setting, secret and preference changes, flagged tool results and tool result policy changes, tools refused by role or mode, file downloads, conversation and memories exports and skill downloads, and refused scheduled actions, from both the web process and the worker. The actor is the warden user; details never carry a value or content. An instance setting and its audit row commit or roll back together; any other failed audit is logged and never breaks the request
- **The worker prunes warden's data** (server mode): the active `spark worker` deletes warden's expired refresh and single-use tokens, second-factor challenges, sign-in throttle windows and single sign-on records every `worker.identity_prune_seconds` (default an hour) and, when `warden.audit.retention_days` is set, purges older audit rows behind a signed checkpoint every `worker.audit_purge_seconds` (default a day)
- **Sign-in and two-step verification screens** (server mode): the sign-in page offers single sign-on when it is configured and links to password reset; a second-factor challenge asks for a code from an authenticator app or a recovery code, with one message for every refused code; where warden requires a second factor the user has not set up, the challenge screen starts the enrolment with a QR code (drawn on the server with reportlab, already a dependency) and the key to copy, and confirming it signs in and shows the recovery codes once, never cached. The challenge stays in its own cookie, its forms carry a CSRF token bound to it, and `next` is followed only to a same-origin path. Personal mode's sign-in is unchanged
- **Invitation, password reset and single sign-on screens** (server mode): an invitation link (`/auth/invitation`) accepts with a password, applies the staged roles and signs the new user in, going on to the enrolment warden requires, or accepts through single sign-on; asking for a password reset (`/auth/password-reset`) answers the same whatever the address, before warden does any work, and a reset link (`/auth/reset`) sets the new password and ends every session. A bad, used or expired link gets one neutral message, and a password warden refuses leaves the link usable. Single sign-on starts at `/identity/auth/sso/start`; the provider returns to `/identity/auth/sso/callback`, and `/auth/sso/complete` exchanges the handoff code on the server with this browser's login binding and signs in, or shows warden's fixed failure message. Without a mail service warden's mails are not sent: a signed-out reset request stays neutral and administrators are shown invitation and reset links once instead (`spark.identity.mail`). A live single sign-on test against mock-oauth2-server is marked `integration`, which CI deselects
- **My account and my API keys** (server mode): `/account` shows the signed-in user's email address, roles and sign-in method, changes their password (ending their other sessions) and sets up an authenticator app when none is confirmed, showing the recovery codes once; replacing a confirmed one is an administrator's reset, as warden 0.1.0 has no self-service replacement. `/account/api-keys`, behind `spark.api_keys`, lists, creates and revokes the user's own keys: a key's scopes may not exceed the permissions of the request creating it, so a key cannot mint a wider key, and the key is shown once. An Account menu lists only the screens the user's roles allow, with sign-out; personal mode's menu is unchanged
- **User administration** (server mode, `spark.users.admin`): `/admin/users` lists users a page at a time, optionally of one status, and creates a user with a password or for single sign-on, with roles; each user's page assigns and revokes roles, suspends, reactivates or deactivates them, resets their two-step verification and sends a password reset link. `/admin/invitations` invites with roles granted on acceptance, resends (the old link stops working) and revokes pending invitations, and `/admin/api-keys` lists and revokes every user's API keys. warden's refusal to leave no active administrator is shown and changes nothing; warden audits every change. Without a mail service the invitation or reset link is shown to the administrator once, never cached or logged
- **Audit trail viewer and export** (server mode): `/audit`, behind `spark.audit.read`, shows warden's audit events newest first, a page at a time, filtered by actor (an email address or user id), action, outcome and dates, with actors shown by email address; `/audit/export`, behind `spark.audit.export`, streams the matching events as CSV or NDJSON through warden's export, which records the export itself. An Auditor reaches the audit trail and no other administration. The container guide describes creating the first administrator, inviting users, two-step verification and single sign-on

### Changed
- **Tools follow the signed-in user's role** (server mode): a tool category is offered and run only when the user's roles grant its `spark.tools.<category>` permission, in conversations, sub-agents, debates, panels, the tool lists and actions run from the web; a request with no signed-in user is refused every built-in tool. The local-only tools stay unavailable on a server whatever the role. Personal mode is unchanged
- **Instance settings follow the signed-in user's role** (server mode): the web start-up replaces the single-operator stub with a check backed by warden permissions, so only a user holding `spark.settings.instance` (an Administrator) may see or change instance settings and secrets; every signed-in user keeps their own preferences. Personal mode is unchanged
- **Dependencies**: `cognisn-warden>=0.1.0b8` (Apache-2.0) from PyPI, the identity, session, permission and audit library for server mode. pip and uv select its beta from the requirement alone, and its own requirements (alembic, argon2-cffi, cognisn-konfig, cryptography, fastapi, httpx, psycopg, pydantic, pyjwt and sqlalchemy) are MIT, BSD or Apache licensed or already accepted by the licence audit
- **Origin pinned to the public address** (server mode): the Origin and Host checks accept exactly the origin of `server.public_base_url` (plus `interface.allowed_origins`), event streams included, never the request's own Host header; personal mode keeps its bind-address rule
- **`server.public_base_url` is required in server mode** (https unless the host is loopback, and an origin with no path): Spark refuses to start without it. warden's own public address is derived from it, so it is set once. Upgrade note: the `warden.url`, `warden.client_id` and `warden.client_secret` placeholders are retired and refused, since warden runs inside Spark; warden's settings are the `warden` section of the configuration (for example `warden.identity.require_mfa`), and `warden.http` is Spark's to derive
- **Strict type checking**: CI also blocks on `mypy --strict` for the rewritten filesystem tool, the process liveness check, the web database accessors, the container's no-nvidia check and the server-mode sign-in endpoints, so every module added or rewritten in 0.3.0 is checked on Linux, macOS and Windows alike
- **Every read is scoped to the signed-in user** (server mode): each repository read and write takes the caller's user guid, and rows reached through a conversation, action or debate turn are scoped through its owner, so another user's id returns nothing and changes nothing. Scheduled actions are listed per user, and an action's runs and tool settings belong to its owner. Endpoints answer another user's conversation, message history, tool activity, agent runs, links, export, debate, panel, action, memory, file or knowledge graph with 404, as for an id that does not exist, and knowledge graph builds, their cancellation and their results are kept per user. In both modes a conversation, action or knowledge graph scope that does not exist now answers 404 where some routes answered 200 without effect. Upgrade note: repository functions that take a parent id now take the user guid after the executor (for example `messages.get_messages(db, user_guid, conversation_id)`)
- **Each chat turn acts for its own user** (server mode): the shared conversation manager passes a turn's user guid to memory tools, the knowledge graph, file tools and sub-agents, and a stream's tool and model approval prompts and agent cancel tokens travel with its turn (`TurnCallbacks`), so concurrent users never share a memory index, an agent's user or a prompt. Personal mode keeps its single local user
- **The worker runs every user's actions as their owner** (server mode): `spark worker` schedules every user's enabled actions and runs each as its owner, with the owner's run records, memories and file area, and tools that follow the owner's warden roles read at run time. An owner without `spark.actions`, a suspended or deactivated owner, or one with no warden user is refused, and the refusal is recorded in the action's run history (status `refused`); run now from the web is refused the same way for a user without `spark.actions`. Neither the web nor the worker uses the process's own user guid on a server any more; data made under the interim single-user guid stays attributable through `spark identity create-admin --link-user-guid`. Personal mode keeps its single local user
- **Action names are unique per user** (both modes): a new revision replaces the global unique action name with one per user, so users of a server never block or learn each other's action names; a clash answers 409 and names only the caller's own action. Downgrading past it refuses while two users share an action name
- **Host-process endpoints are refused on a server**: `/api/quit`, `/api/open-folder`, `/settings/api/lock` and `/settings/api/unlock` answer 404 in server mode, and the MCP server settings (`/settings/mcp/*`, which write `config.yaml`) need `spark.settings.instance`. Personal mode is unchanged
- **`spark worker` resolves a `secret://` database URL** (server mode): the worker configures its runtime with konfig's secrets before any database or tool work, as `spark web` and `spark migrate` do, so a `database.url` kept in the secrets store no longer stops it starting
- **Pending chat interactions belong to their user** (server mode): a stream's tool permission prompts, agent model approvals, agent cancel tokens and the stream itself are bound to the user whose turn raised them, so `/chat/permission/respond`, `/chat/agent/model-approve`, `/chat/agent/cancel` and `/stream/cancel` answer another user's id, or one that has ended, with 404 and change nothing. Personal mode is unchanged
- **Actions and skills follow the user's role** (server mode): creating, editing, toggling, running now and AI-creating actions need `spark.actions` (a Power user's), and a User is refused with 403 while still listing their own actions; importing, deleting and reloading skills, shared by every user, need `spark.settings.instance`, while each user still turns skills on or off for themselves. Personal mode is unchanged
- **Sub-agents need `spark.agents`** (server mode): spawning a sub-agent needs `spark.agents` wherever the spawn comes from (a chat turn, an action or another agent), beyond the `spark.tools.agents` permission that offers the tool; a refused spawn returns a tool error and starts nothing. Personal mode is unchanged
- **Sub-agents and the action assistant choose only offered models** (server mode): when `models.offered` is set, a sub-agent may be spawned only with an offered model (one spawned without a model keeps using its conversation's, even one that has left the list), the models listed to the LLM for sub-agents and for their approval prompt are only those offered, and the action creation assistant may create an action only with an offered model. `LLMManager.is_offered()` answers by the model list's own rule. Creating a conversation on a server no longer sets the shared manager's model, which every user's turns share. Personal mode is unchanged
- **The models offered are enforced where a model is chosen** (server mode): when `models.offered` is set, creating a conversation (its debate and panel agents included), changing a conversation's model, creating or editing an action, AI-creating an action, approving a sub-agent's model and the default model preference refuse a model that is not offered with 409 and a message naming it. A conversation pinned to a model that has left the list keeps working; its info carries `model_offered: false` and `model_notice`, and the chat page shows the notice. AI-creating an action answers through a service of its own for the chosen model instead of changing the shared manager's model, so one user's choice never changes another's. Personal mode, which has no list of models offered, is unchanged
- **Each chat turn answers through a model service of its own** (both modes): a turn no longer sets its conversation's model on the shared LLM manager and then invokes the manager's active provider, so two users' turns on different models at once each answer with their own model. `LLMManager.service_for()` gives a copy of the provider that lists the model, with the model set on the copy (sharing the provider's client and caches), and the turn uses it for invoking, counting tokens, the tool loop, prompt caching and compaction; the shared manager's provider and model never move. A conversation pinned to a model that has left `models.offered` keeps working, and a model no provider lists still falls back to the active provider. A locked compaction model (`compaction_model`) now compacts with a service of its own for that model, and a sub-agent, debate or panel service whose provider cannot be cloned is a copy, never the shared provider changed in place. The models listed for sub-agents come from the provider of the conversation's own model. A single user in personal mode sees no difference
- **The default model is the signed-in user's** (server mode): `/conversations/api/models` names the user's `default_model.model_id` preference (else the instance's), only when it is offered, and `default_model.mode` comes from the instance settings, never from `config.yaml`. A knowledge graph build without `knowledge_graph.build_model` extracts with the user's default model (in personal mode `config.yaml`'s), else the first model listed, through a service of its own, never whatever the shared manager last had; with no model at all the build is refused with 409. Personal mode's model list is unchanged
- **Developer guide**: `CLAUDE.md` lists the identity rules for server mode (how to find the user, check a permission, scope a repository call and record an audit event) and the shared modules for the next wave

## [0.3.0a9] - 2026-11-02

### Added
- **Untrusted content for files and commands** (personal mode): results of the filesystem read tools (`read_file`, `find_in_file`, `search_files`, `list_directory`, `get_file_info`, `get_directory_tree`) and system command output (`run_command`) now reach the model as marked, untrusted data and are inspected like web, MCP, document and sub-agent results. Each has its own action on a prompt injection or jailbreak match, `embedded_tools.tool_results.files` and `embedded_tools.tool_results.commands` (mark, warn or withhold; default warn), set on the personal settings page with the other result actions. A server never offers these tools, so it hides and refuses both settings

### Changed
- **Filesystem tool containment** (personal mode): the filesystem tools (`read_file`, `write_file`, `list_directory`, `search_files`, `get_file_info`, `find_in_file`, `get_directory_tree`) now use the same resolved-path check as the document and archive tools, so every file tool follows one containment rule over the configured allowed paths. Symlinks are resolved; `..` components, absolute paths outside the allowed paths, Windows drive, UNC and device forms, reserved device names and alternate data streams are refused with a clear message. A relative path is taken against the first allowed path rather than the process's working directory. Listings, trees and searches never show or descend into an entry that lies outside the allowed paths, and a search pattern may not leave the directory being searched

## [0.3.0a8] - 2026-11-02

### Changed
- **File areas at start-up**: `spark web` now sets up the file area from `files.root` while it validates the start-up configuration, so uploads and the file tools are available in the running app in both modes (they were set up only on the older start-up path)
- **Container image**: the build fails if any `nvidia-*` package is installed (`scripts/check_no_nvidia.py`, run after Spark is installed on the CPU-only PyTorch wheel), and `deploy/check-image.sh` checks a built image for them too. The image prepares `/var/lib/spark-files` for users' file areas
- **Compose stack**: users' file areas live on a named `spark-files` volume shared by the web and worker roles (`SPARK__FILES__ROOT=/var/lib/spark-files`), and the shared secrets file is named on the `spark-data` volume (`SPARK__SECRETS__FILE_PATH`), so every container reads the same one. [Running Spark in containers](docs/container.md) names the pgvector minimum (0.8 or later), the new volume and running several web replicas
- **Dependencies**: `cognisn-konfig[aws]>=0.4.0` (konfig with its AWS extra, for the AWS Secrets Manager secrets backends in server mode); konfig is MIT and its extra adds only boto3 (Apache-2.0), which SPARK already uses
- **Strict type checking**: CI now blocks on `mypy --strict` for the 0.3.0 modules added or rewritten since 0.3.0a1 (the vector store and embeddings, knowledge store, instance settings and their consumers, file areas, web security, sign-in and sessions, the files, settings and sign-in endpoints, tool registry and command policy, the file tools, untrusted tool results and the LLM manager)
- **Embeddings**: the web start-up warm-up and the knowledge graph tools construct `spark.index.SentenceTransformerEmbedding`. The 0.2.0 class name `spark.index.embeddings.EmbeddingModel` is retired; `spark.index.EmbeddingModel` is the embedding protocol only (upgrade note: code that constructed `EmbeddingModel()` constructs `SentenceTransformerEmbedding()` instead)
- **Server-mode web process**: the web role no longer starts the tray daemon in server mode (scheduled actions run in `spark worker`); personal mode starts it as before when actions and the daemon are enabled
- **Settings propagation in the running app**: the web lifespan starts the instance settings listener in server mode once the database is open, and stops it before the database closes, so an administrator's change on one web replica reaches every other replica without a restart. A replica whose listener cannot connect at start-up refuses to serve. Personal mode installs the `config.yaml` settings source as before
- **Server mode follows the instance settings** (`spark.instance_settings.consumers`): the LLM providers, the models offered, the conversation manager's embedded tools configuration (with prompt inspection) and the tool execution limits are read from the administrator's instance settings and secrets store, and follow every change without a restart. Providers are rebuilt only when a provider setting or credential changes; the model list shows only the models offered when that list is set. Local-only tool categories are left out of a server's tools configuration. Personal mode keeps reading `config.yaml`
- **Tool result actions as administrator policy** (server mode): the action for a flagged result from web, MCP, document and sub-agent tools (`embedded_tools.tool_results.<source>`: mark, warn or withhold) is an instance setting, shown under Security on the administrator's instance page and saved through `/settings/api/instance`. Each change is reported to the audit hook as `tool_result.policy_changed` (key, source, action, previous action, actor). Users cannot set it as a preference, and the personal settings endpoint does not accept it on a server. Personal mode keeps the setting on its own settings page
- **Local-only tools leave a server's settings**: the system command, filesystem and email settings no longer appear on a server's instance page or in its view of the settings registry and secrets (`spark.instance_settings.registry.offered_keys`), and saving them is still refused
- **Theme on a server** is each user's own preference: `/api/theme` reads and saves the signed-in user's `interface.theme` in server mode instead of writing `config.yaml`

## [0.3.0a7] - 2026-10-30

### Added
- **Worker role** (server mode): `spark worker` runs scheduled actions away from the web processes. Several workers may run against one database; one is active, holding a PostgreSQL advisory lock, and the others stand by and take over when it stops or loses its database connection. The worker refuses to start in personal mode, on SQLite, or on a database behind the current schema (run `spark migrate` first). Later releases add background duties through a registration point for periodic jobs and long-running services
- `spark web` starts the web interface; plain `spark` remains an alias for it
- **Container image**: a multi-stage `Dockerfile` (Python 3.12 slim, CPU-only PyTorch, no compiler or pip in the final image) running as the unprivileged user `spark` (uid 10001), with a healthcheck on the web role and volumes for data and the embedding model cache. The licence audit runs during the build. `deploy/check-image.sh` checks a built image (non-root, no build tools, healthcheck, no tests)
- **Compose stack** (`deploy/compose.yaml`): PostgreSQL with pgvector, a one-shot `spark migrate`, the web role and the worker role; the web port is published on 127.0.0.1 by default and credentials come only from an env file the operator writes (`deploy/spark.env.example`)
- **Image workflows**: CI builds and checks the image on every push and pull request; the Container publish workflow pushes to ghcr.io from a `v*` tag or a manual run, tagged only with a release candidate or final version (`0.3.0rcN`, `0.3.0`), never `latest`
- [Running Spark in containers](docs/container.md): build, roles, Compose and upgrades

### Changed
- The tray daemon and the background action daemon refuse to start in server mode, with a message pointing to `spark worker`. Personal mode is unchanged

## [0.3.0a6] - 2026-10-30

### Added
- **Per-user file areas** (`spark.files`): each user's files live in their own area behind a storage interface (`FileStore`); 0.3.0 stores them on a directory or mounted volume set by `files.root`, which defaults to `files` in the data directory in personal mode and must be set in server mode. Keys are always relative to the user's area
- **Storage quotas and upload limit**: `files.quota_mb` (per user; default 1024 in server mode, no quota in personal mode) and `files.max_upload_mb` (default 50); `0` means no limit. A file that would exceed either is refused with a message saying which limit applies, and nothing is stored. Usage is charged in the same database transaction that records the file, so parallel uploads cannot overrun a quota. New tables `user_files` and `user_file_usage` (migration `file_areas`)
- **Files API** (`/files/api`): upload a file (optionally to one of your conversations), list your files with your usage and limits, download and delete them. Every route is scoped to the signed-in user; another user's file is reported as not found. Downloads are always sent as attachments

### Changed
- **File tool containment**: the document, document creation and archive tools read and write only inside the signed-in user's file area in server mode (paths are relative to the area, and documents and extracted files count towards the quota), and only inside the configured allowed paths in personal mode. Every path is checked once, after resolving symlinks; `..` components, absolute paths outside the permitted area, Windows drive, UNC and device forms, reserved device names and alternate data streams are refused. Images embedded in created documents come from the same permitted area. In server mode these tools no longer need `allowed_paths`
- **Archive extraction** checks every member before writing anything: an archive with a member that would land outside the destination, or a link or special file member, is refused as a whole

## [0.3.0a5] - 2026-10-30

### Added
- **Instance settings** (`spark.instance_settings`): a typed registry of every setting SPARK accepts (key, scope, default and validation), and one accessor to read a setting in either mode. In server mode the administrator's non-secret settings (providers and models offered, MCP servers, tool policy actions, email and ElevenLabs switches, file quotas) live in a new `instance_settings` table and each user's preferences (default model, system instructions, theme, voice) in `user_preferences`; personal mode keeps `config.yaml`. Run `spark migrate` in server mode for the new tables
- **Settings propagation between web replicas**: a change made on one replica reaches the others over PostgreSQL LISTEN/NOTIFY; a dropped listening connection is re-established with back-off and every setting reloaded once it is back
- **Start-up configuration** validated before SPARK serves anything: the database URL (a URL or a `secret://` reference resolved through konfig), `server.public_base_url`, placeholder `warden` keys, and the secrets store. Server mode refuses the OS keychain and needs konfig's encrypted store with `KONFIG_MASTER_KEY` (`secrets.backend: encrypted_file`) or AWS Secrets Manager (`secrets.backend: aws_secrets_manager`, or `KONFIG_AWS_SECRETS_MANAGER`); each refusal names the setting

- **Settings pages in server mode**: administrators get an instance settings page (providers, models offered, tools, integrations, quotas) whose changes apply to every replica, and every user gets a "My preferences" page (default model, system instructions, theme, voice). Until warden is integrated the operator signed in with the start-up code is the administrator. Personal mode keeps the one settings page

### Changed
- **Secrets on the settings page are write-only**: each is shown as set or not set and can be replaced or cleared; no stored value is sent back to the browser
- **Settings are validated before anything is saved**: an unknown key, a setting of the wrong kind for the page, or an invalid value refuses the whole save with a message naming the setting, and nothing is written
- **Secret names now include their group**: `database.url` is stored as `database_url` (it was stored as `url`), `providers.anthropic.api_key` as `providers_anthropic_api_key`, and so on. On start-up (and in `spark migrate`) existing entries are moved to the new names and the references in `config.yaml` rewritten; secrets that 0.2 kept as plain text in `config.yaml` (the web search API keys) are moved into the secrets store

### Fixed
- A PostgreSQL URL saved on the settings page was stored as a `secret://` reference that start-up never resolved, so server mode refused to start with it

## [0.3.0a4] - 2026-10-30

### Added
- **Untrusted content in tool results**: results from web search and fetch, MCP tools, documents and archives, and sub-agents now reach the model inside marked untrusted-content blocks with a delimiter that is random for each turn, and the system prompt tells the model to treat their contents as data, never instructions. This applies to chat, sub-agents, debates, panels and scheduled actions. Each result is also checked for prompt injection and jailbreak patterns; for each source (web, MCP, documents, sub-agents) Spark marks a match, warns the model about it (the default) or withholds the result and tells the model why. Matches are shown in the chat sidecar and recorded with the other prompt inspection violations. Personal mode sets the actions under Settings, Security; a server reads them from `embedded_tools.tool_results.<source>`
- **Tool availability by mode**: in server mode the system command, filesystem and email tools are not offered at all. They are refused by name wherever a request comes from (a model turn, a sub-agent, a scheduled action), their settings sections are hidden, and the settings API refuses to save their settings, test the email connection or browse the server's directories. Personal mode keeps them as before. Every built-in tool now belongs to a category (`spark.tools.<category>`), and one registry function decides whether a category is available
- **Command blocklist across chained commands** (personal mode): the system command tool now checks every command in a command line against the blocklist, not only the first. It understands the chaining and nesting forms of each shell: `;`, `&&`, `||`, `|`, `&`, newlines, backticks, `$( )`, groups and `bash -c` on macOS and Linux; `&`, `|`, groups and `cmd /c` for cmd.exe; and `-Command` and `-EncodedCommand` payloads, `;`, script blocks and `Invoke-Expression` for PowerShell. A command line it cannot split with confidence (an unclosed quote or substitution, or a command name built from a variable or a wildcard) is refused with a message saying why

## [0.3.0a3] - 2026-10-30

### Added
- **Sign-in codes (personal mode)**: each code works once and expires after five minutes (`interface.login.code_ttl_seconds`), including the code in the link opened in the browser at start-up. Loading the sign-in page when no code is live shows a new one in the terminal. An address that fails five times within five minutes is locked out for fifteen (`interface.login.max_failures`, `failure_window_seconds`, `lockout_seconds`); failed attempts are logged with the address, never the code
- **Origin and CSRF checks**: every POST, PUT, PATCH and DELETE must come from Spark's own pages and carry the session's CSRF token, which the pages send automatically; chat, debate and panel event streams must come from Spark's own pages. A refused request gets a 403 saying which check failed. Further origins (for example a reverse proxy's public address) can be allowed with `interface.allowed_origins`
- Sessions also end 24 hours after sign-in, however active (`interface.session_max_age_hours`), alongside the existing inactivity timeout

### Changed
- The session cookie is `SameSite=Strict`, and `Secure` when Spark is served over HTTPS. Following a link to Spark from another site opens the sign-in page
- Signing out is a POST from the dashboard's Sign Out button, and ends the session on the server; `GET /logout` is no longer served
- Scripts that call the web API directly must send an `Origin` header naming the instance and an `X-CSRF-Token` header with the session's token

## [0.3.0a2] - 2026-10-30

### Added
- **Vector search in server mode**: memories and conversation context are searched in PostgreSQL with pgvector (an HNSW cosine index), filtered to the signed-in user in the same query. `spark migrate` creates the `vector` extension if it is missing (the database role needs the right to create it, or an administrator creates it first) and copies existing embeddings into the new tables. Personal mode keeps its SQLite storage and search unchanged
- **Embedding and vector store interfaces** (`spark.index.EmbeddingModel`, `spark.index.VectorStore`): the local sentence-transformers model on the CPU is the one implementation for now, so other embedding providers can be added later. Each stored vector records its dimension; after a change to an embedding model of another dimension, adding or searching is refused with a message asking for the index to be rebuilt, and the memory and context indexes gain `rebuild()`

### Fixed
- Knowledge graph entities and relationships, skill states and per-conversation skill states could be duplicated when two requests created the same one at the same moment (seen on PostgreSQL). They are now unique on their natural keys and merged in one statement. Upgrading merges existing duplicates, keeping the earliest entry: entity and relationship weights are added together and relationships are moved onto the kept entity, and skills keep their most recent on or off state

### Changed
- The installation guide shows how to install the CPU-only PyTorch on Linux (`uv tool install cognisn-spark --torch-backend cpu`, or PyTorch from its CPU index before `pip install cognisn-spark`), avoiding about 2.4 GB of unused NVIDIA CUDA packages
- New runtime dependency `pgvector` (MIT), the psycopg adapter for PostgreSQL's `vector` type, used in server mode

## [0.3.0a1] - 2026-10-29

### Added
- **Deployment mode**: a `mode` setting (`personal` or `server`, environment `SPARK__MODE`) read once at start-up through konfig. Personal mode keeps today's defaults (SQLite in the platform data directory). Server mode requires `database.url` to be a PostgreSQL URL and refuses to start otherwise
- **Versioned migrations**: an Alembic chain for SPARK, kept in a `spark` PostgreSQL schema with its own version table (the default schema on SQLite). The baseline reproduces the 0.2.0 schema exactly. `spark migrate` runs the chain (and, from a later release, warden's)

### Changed
- **Upgrading from 0.2.x (personal mode)**: on first start SPARK upgrades the existing SQLite database once: it applies 0.2.0's own schema statements, checks every column and index against the 0.2.0 baseline, and stamps the database. If anything differs it refuses with a list of the differences and changes nothing. Later schema changes are applied automatically at start-up in personal mode; in server mode run `spark migrate` first (SPARK refuses to start on a database behind the current schema)
- **Async data layer** (`spark.db`): a psycopg 3 connection pool for PostgreSQL and a WAL-mode aiosqlite pool for SQLite, replacing the single shared connection whose cursors could be read by another thread (the knowledge-graph status race). Synchronous code reaches it through one documented bridge. New runtime dependencies: `aiosqlite`, `psycopg[binary,pool]` and `alembic`
- README installation instructions now cover PyPI and uv only; the standalone download table is replaced by a note that standalone builds are paused
- The Database settings page offers the mode (personal or server) and, for server mode, the PostgreSQL URL (stored in the keychain); host, port, user and MySQL or SQL Server choices are gone

### Removed
- The `html2text` dependency (GPL-3.0-or-later). The web fetch tool converts pages to Markdown with `markdownify` (MIT), keeping links and headings and dropping images, scripts and styles as before
- **MySQL, MariaDB and SQL Server support**, the psycopg2 driver, and the `mysql`, `postgresql`, `mssql` and `all-databases` extras. Upgrade note: personal mode uses SQLite as before; PostgreSQL is supported in server mode (`mode: server` with `database.url`), through psycopg 3, which is now a standard dependency. A configuration that still sets `database.type` to `mysql`, `mariadb` or `mssql` is refused at start-up with a message saying so

### Fixed
- On Windows, checking whether the action daemon or the tray was already running sent a Ctrl+C to the console it shared (`os.kill(pid, 0)` is a Ctrl+C there); the check now asks Windows for the process state instead
- Windows path handling for skill resources
- The knowledge graph status page could fail with an error while a build was running, because the build and the status request shared one database connection
- Web requests no longer block the server while they read or write the database, and a slow conversation turn no longer delays other requests' database work
- Scheduled actions no longer open a new database connection (and re-check the schema) for every run and every tool call; the tray daemon reads its statistics through the same connection pool instead of opening the database file directly
- Stale scheduled-action locks are now released after 30 minutes as intended (the age check compared timestamps in two different text formats)

## [0.2.1] - 2026-10-28

0.2.0 was released on GitHub but did not reach PyPI. 0.2.1 is the first PyPI release of the 0.2 line and contains everything listed under 0.2.0.

### Fixed
- Releases publish to PyPI again. The pinned PyPI publish action used a twine that rejects the wheel metadata (version 2.5) written by current hatchling; it is now pinned to v1.14.3. The publish step no longer ignores errors, so a failed upload fails the release run, and re-releasing an existing version is skipped instead

## [0.2.0] - 2026-10-28

First stable release since 0.1.0. It consolidates the betas 0.2.0b1 to 0.2.0b17, released between 15 April and 26 July 2026, and is distributed through PyPI only: install it with `pip install cognisn-spark` or `uv tool install cognisn-spark`.

### Added
- **Conversation types**: conversation creation offers a type selection (Standard chat, Debate or Panel) as the framework for further chat styles
- **Debate mode**: a Pro and an Against agent, each with its own model, brief and full tool access, argue a user-defined topic across rounds and present structured evidence exhibits, while a Judge agent moderates, rules on the presented arguments only and answers follow-up questions; three-pane UI with per-turn cancellation and pause and resume
- **Debate agent capabilities**: per-agent tool and skill allowlists chosen at debate creation (judge: skills only), enabling asymmetric debates
- **Debate view polish**: per-pane chat-style tool activity groups, auto-scroll toggles, a highlighted Final Judgement card, Markdown and HTML export, and a floor indicator glow on the active agent's pane
- **Panel conversations**: 2 to 5 named AI panellists (per-panellist models, briefs and capability allowlists) plus an optional human panellist discuss a topic in moderator-directed rounds with shared visibility, a moderated synthesis, follow-up questions, a threaded discussion UI with a speaker rail, and Markdown and HTML export
- **Skills**: Claude-compatible SKILL.md skill folders with bundled resources; enabled skills are advertised in every LLM surface (chats, sub-agents, autonomous actions, debates) and loaded on demand via `use_skill`; `/skill-name` explicit trigger; approval-gated `create_skill` and `update_skill` authoring behind a bundled skill-creator skill; Skills management page with import, export and per-conversation toggles
- **Knowledge graphs**: two-level knowledge graphs built on demand by LLM extraction: a global graph over all conversations and memories, optional per-conversation graphs (a creation-time option), per-conversation opt-out of the global graph, and link-based pooling of conversation graphs; queryable by the model via `query_knowledge_graph` with automatic injection of relevant subgraph context; Knowledge page with build controls and an interactive force-directed visualisation
- **ElevenLabs voice (optional)**: natural text-to-speech as an opt-in integration. Enable it in Settings with an API key (stored in the OS keychain, never sent to the browser) and voice mode speaks with natural voices instead of the browser synthesiser, which remains the default and the automatic fallback on any error, quota exhaustion or missing key. Voice mode now works in debate and panel as well as chat, with a distinct voice per agent chosen at creation, three interaction modes (listen-along, immersive, listen-only), a monthly character cap, and an audio cache so repeated text is never billed twice
- **Run with uv**: Spark can be installed and run with `uv tool install cognisn-spark` (or `uvx cognisn-spark`); a `cognisn-spark` command alias makes the natural `uvx` invocation work. New `interface.port` and `interface.open_browser` settings (overridable via `SPARK__INTERFACE__PORT` and `SPARK__INTERFACE__OPEN_BROWSER`) allow a fixed port and a suppressed browser for headless and server runs. A CI smoke test installs the built wheel with uv and confirms it launches
- **System command tool**: execute shell commands (git, docker, aws, curl and so on) with OS-aware execution, a blocked command list and configurable approval prompts
- **Agent spawning**: the LLM can spawn independent sub-agents via the `spawn_agent` tool, with a dedicated Agents tab in the resizable sidecar panel; supports orchestrator-workers and chain modes; model auto-selection with user approval
- **Email tool**: send and draft emails via SMTP with HTML or plain text, to, cc and bcc, and file attachments; SMTP test connection button; passwords stored in the OS keychain
- **Document creation**: create Word (.docx), Excel (.xlsx), PowerPoint (.pptx) and PDF documents with advanced formatting (headings, tables, lists, images, styles)
- **Provider setup guides**: in-app setup guides for all LLM providers (Anthropic, AWS Bedrock, Ollama, Google Gemini, X.AI) with step-by-step instructions
- **Conversation to action**: create autonomous actions directly from conversations with AI-guided setup
- **AWS Bedrock API key authentication**: explicit access key, secret key and session token authentication alongside SSO
- **Windows code signing**: the release workflow signs the Windows executable and NSIS installer with SSL.com eSigner (binary builds are currently disabled; see Changed)
- **4-level tool approval**: Deny, Approve Once, Always (Conversation) and Always (Global), with a `global_tool_permissions` table
- **MCP UI enhancements**: edit MCP server configurations, view tools from the dashboard, and a manage servers link
- **Tool activity date grouping**: sidecar entries grouped by date with collapsible headers and call counts
- **Resizable sidecar**: Tools and Agents tabs with a drag-to-resize handle; the width persists in the session
- **Run now**: execute autonomous actions immediately from the Actions page
- **Theme persistence**: the dark or light theme preference is saved to config.yaml and persists across restarts
- **Directory browser**: filesystem `allowed_paths` uses a folder browser modal instead of a text input
- **Tool documentation**: documentation for `run_command`, `spawn_agent`, `list_provider_models`, `create_word`, `create_excel`, `create_powerpoint` and `create_pdf`
- Development tooling: `scripts/stamp_version.py` stamps a version and moves `[Unreleased]` into a dated changelog section, refusing a version that does not follow the current one under PEP 440; a worktree recipe at `dev/worktree.json`; contributor rules and project configuration in `CLAUDE.md`; `.gitattributes` enforcing LF line endings

### Changed
- **Releases are PyPI only**: the standalone macOS and Windows binaries, DMG and NSIS installers are not built for this release
- **Skill loading is transparent**: loading a skill (`use_skill` or `read_skill_resource`) no longer pauses for approval; it runs silently as a read-only progressive-disclosure step, in chats, debates, panels and sub-agents. Authoring skills (`create_skill` or `update_skill`) is still approval-gated
- **Debate and panel capabilities honour global skill settings**: the per-agent capability checklists in the debate and panel creation wizards list only skills that are enabled in the global settings, so a globally disabled skill no longer appears there (tools were already gated by their category settings)
- **Per-conversation skills control is easier to find**: in a conversation's tools panel, the Skills section is a distinct, labelled card with an enabled count and a note that it defaults to your global settings
- Autonomous action system prompt includes tool context, filesystem paths and OS information
- Autonomous action max_tokens guidance reflects the configured limit
- Autonomous action tool iterations increased to 25 (was 10)
- Action run history records the full activity log (tool calls and results)
- Max_tokens truncation triggers an automatic retry with a concise output instruction
- Prompt caching enabled for the daemon action executor
- CI now runs on pushes to release and workstream branches and on pull requests to release branches, as well as on `main`
- The `web_fetch` and `web_search` tool documentation now uses LF line endings, like the rest of the tree

### Fixed
- Debate and panel turns could fail with "No submit_argument call" (or discard a panellist's contribution) when the agent spent its whole tool budget researching, often after repeated web searches returned no results, and never reached the final submission tool. Agents now get a firm, tool-restricted prompt to submit before a turn is abandoned, and debaters and panellists have a larger research budget (25 tool iterations), so heavy research no longer throws the turn away
- Debate and panel modes now use prompt caching (on providers that support it: Anthropic, AWS Bedrock and Google Gemini) for every debater, panellist, judge and moderator turn. The system prompt and tool definitions are cached and reused across a turn's tool calls and across rounds, cutting token cost and latency. Previously these turns never requested caching, even with it enabled globally; they now follow the same `conversation.prompt_caching` setting as normal chats
- AWS Bedrock now honours prompt caching. The Converse provider previously ignored the caching flag entirely; it now inserts `cachePoint` blocks on the system prompt and tool definitions, reports cache read and write token metrics, and safely retries without caching for models that do not support it
- Debate and panel turns failed with "temperature is deprecated for this model" (HTTP 400) on the Claude 5 family (Sonnet 5, Fable 5) and Opus 4.7 and 4.8. The Anthropic Direct provider no longer sends the `temperature` parameter to models that reject it, and defensively retries without it if a model returns 400 on temperature
- Email settings `use_tls` and `require_approval` were never coerced to booleans, because a duplicate `_BOOL_STRING_KEYS` declaration silently overwrote the first
- Home page conversation lists now open debate and panel conversations in their own views (they were opening the standard chat view)
- Debate view now renders markdown in all three panes (was plain text)
- MCP stdio tool execution failing during conversations (event loop mismatch)
- Memory storage and dashboard using the wrong user GUID
- Tool activity sidecar empty when reopening past conversations
- Filesystem `allowed_paths` stored as a string instead of a list
- macOS app PATH missing common tool directories (Homebrew, Docker)
- Daemon MCP connections: independent connections per action execution
- Scheduler timezone: cron schedules now use the local timezone
- Scheduler sleep and wake recovery with stale lock clearing
- Email TLS string normalisation and secret resolution
- Agent provider isolation: concurrent conversations no longer conflict
- Settings tool configuration refreshes without a restart
- SSL auto-generates a self-signed certificate when enabled without certificate files

### Beta history
- 0.2.0b1 to 0.2.0b11 (15 April to 10 July 2026): system command tool, agent spawning, email and document creation tools, provider setup guides, AWS Bedrock key authentication, 4-level tool approval, sidecar and MCP UI improvements, and scheduler, daemon, MCP and settings fixes
- 0.2.0b12 (10 July 2026): conversation types, debate mode, panel conversations, skills and knowledge graphs
- 0.2.0b13 (14 July 2026): optional ElevenLabs voice; email settings and home page fixes
- 0.2.0b14 (23 July 2026): run with uv; releases become PyPI only
- 0.2.0b15 (25 July 2026): temperature fix for models that reject the parameter
- 0.2.0b16 (25 July 2026): transparent skill loading and skills configuration changes
- 0.2.0b17 (26 July 2026): forced final submission in debate and panel turns; prompt caching for debate, panel and AWS Bedrock

## [0.1.0] - 2026-04-06

First production release of Spark -- Secure Personal AI Research Kit.

### Added

#### LLM Providers
- **Anthropic** (Direct API) -- Claude models with prompt caching support
- **AWS Bedrock** -- Claude and other foundation models via AWS
- **Google Gemini** -- dynamic model discovery from Gemini API
- **Ollama** -- local model support with automatic model detection
- **X.AI (Grok)** -- Grok models via OpenAI-compatible API
- Dynamic model discovery from provider APIs with static fallback
- Model list caching across all providers
- Transient error retry (503, 500, rate limits) with exponential backoff
- Prompt caching for Anthropic (cache_control blocks on system prompt and tools)
- Per-conversation prompt caching toggle with global default
- Cache stats displayed in UI (tokens read, created, savings percentage)

#### Conversations
- Real-time streaming via Server-Sent Events (SSE)
- Intelligent context compaction (LLM-driven summarisation)
- Conversation linking for shared context across conversations
- RAG retrieval from compacted history (filters active messages to avoid duplication)
- Favourites with star toggle
- Export as Markdown, HTML, CSV, or JSON
- Per-conversation settings: model, instructions, RAG, compaction thresholds
- Model badge on each assistant response showing which model generated it
- Model selector modal with search, grouping by provider, tool support indicators
- Intermediate tool-use messages preserved in separate bubbles

#### Tools
- **Tool Activity Sidecar Panel** -- dedicated right-side panel for tool calls with timestamps, expandable parameters and results
- Inline "Used N tools" indicator in chat linking to sidecar
- **Built-in tools:** filesystem (read/write), documents (Word, Excel, PDF, PowerPoint), archives (ZIP, TAR), web search/fetch, datetime
- **Web search engines:** DuckDuckGo (default, no API key), Brave Search, Google (SerpAPI), Bing (Azure), SearXNG (self-hosted)
- **Tool documentation system:** 22 markdown docs, get_tool_documentation tool for LLM self-reference
- **MCP integration:** stdio, HTTP streamable, and SSE transports with hot-reload
- Per-conversation tool enable/disable for embedded and MCP tools
- Category-based tool approval (approving web_search also approves web_fetch)
- Tool permission system with Allow Once, Always Allow, and Deny

#### Memory
- Persistent memory with semantic vector search (sentence-transformers)
- Categories: facts, preferences, projects, instructions, relationships
- Auto-retrieval of relevant memories injected into conversation context
- Proactive memory storage when user shares information
- Import/export as JSON
- Memory management page with stats, edit, delete, and delete-all

#### Voice
- **Speech-to-text input** -- mic button for manual dictation
- **Voice Conversation Mode** -- full hands-free operation via headset button
- Auto-send after 1.5s silence, TTS reads response, auto-resumes listening
- Voice selector dropdown with all available browser TTS voices (persisted)

#### Autonomous Actions
- Scheduled AI tasks with cron or one-off schedules
- AI-assisted action creation (describe what you want, Spark builds it)
- Context modes: fresh (clean each run) or cumulative (previous results in prompt)
- Failure tracking with auto-disable after threshold
- Run history with status, results, and token usage
- System tray daemon (macOS, Windows) with stats polling

#### Web Interface
- Cognisn design system (Bootstrap 5.3, dark/light theme with persistence)
- Dashboard with provider status cards and clickable provider models modal
- Settings page with providers, database, interface, conversation, security, tools, MCP
- Global system instructions
- Settings lock with password protection
- Secret reveal (eye toggle fetches actual value from server)
- Built-in searchable user guide
- Auto-update checker with rendered markdown release notes and download link
- Keyboard shortcuts

#### Security
- Prompt inspection with pattern matching (injection, jailbreak, PII detection)
- Configurable levels (basic, standard, strict) and actions (block, warn, log_only)
- Session-based authentication with cookie security (HTTPOnly, SameSite, Secure)
- API keys stored in OS keychain via cognisn-konfig

#### Platform
- **macOS:** signed and notarized DMG (ARM64, x86_64) with native splash screen
- **Windows:** NSIS installer with clean upgrade handling
- **Linux:** pip install
- PyApp distribution with Cognisn fork splash screen (v0.29.0-splash.4)

#### Database
- SQLite (default), MySQL, PostgreSQL, MSSQL
- Idempotent schema with automatic migrations

#### Documentation
- 16 comprehensive docs with Mermaid diagrams
- Covers installation, configuration, providers, conversations, tools, MCP, memory, actions, voice, web search, security, auto-update, architecture, development, API reference

#### Quality
- 398 automated tests across 19 test files
- SonarCloud: all A ratings (0 bugs, 0 vulnerabilities, 0 hotspots)
- CI on Ubuntu, macOS, Windows (Python 3.12, 3.13)

[0.2.1]: https://github.com/Cognisn/spark/releases/tag/v0.2.1
[0.2.0]: https://github.com/Cognisn/spark/releases/tag/v0.2.0
[0.1.0]: https://github.com/Cognisn/spark/releases/tag/v0.1.0
