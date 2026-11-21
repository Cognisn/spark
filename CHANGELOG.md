# Changelog

All notable changes to Spark will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0rc27] - 2026-11-21

### Added

- **Discovered models show where they are processed, and why any is refused.** On the Connections page each connection's models view lists every model the service lists, with the Regions it is processed in, whether it is usable under the connection's residency and Region choice, and conduit's reason when it is refused. Differences between the catalogue and the listing (conduit's drift: no capability record, not listed, limits or capabilities differ, Regions declared) are shown as information and never hide a model. A model the service lists is no longer left out because the catalogue lacks its Region.
- **An administrator chooses the Regions for each connection.** A new Regions action on a connection offers conduit's documented lists for its service and sets the adapter's own setting: Bedrock processing (AWS Regions and global), Anthropic `inference_geo`, OpenAI `data_residency`, and a Foundry deployment (type and Azure Region). conduit validates the choice and its refusal is shown with nothing saved; the change is audited by field name only. The choice only narrows the deployment's residency policy, and a connection that chooses none is unchanged. `docs/deployment/aws.md` describes it for Bedrock.

### Changed

- **Adopted conduit 0.1.0** (`cognisn-conduit>=0.1.0`). Models are checked against the connection's provider settings (so its Region choice applies), discovery uses conduit's `uncatalogued`, `catalogued` and `drift` in place of the removed `undeclared`, and each provider is built on the connection's catalogue with the Regions and limits the service lists for each catalogued model, because the default catalogue no longer carries Region data (without this a Bedrock connection under a Regions policy offered no models). A failed or unsupported listing builds on the catalogue alone. A model the catalogue does not declare is still declared first, as before.
- **`bootstrap.bedrock.residency_regions` also sets the connection's Bedrock processing choice** (the listed Regions, without global). A Region conduit refuses (one where Bedrock's runtime endpoint is not offered) fails bootstrap with conduit's reason. An existing `aws-bedrock` connection is never changed.

## [0.3.0rc26] - 2026-11-20

### Fixed

- **The bootstrap marker no longer logs a warning on each settings load.** Bootstrap records that it has dealt with the Amazon Bedrock connection in an instance settings row (`bootstrap.bedrock.seeded`), which is not a setting. The registry now declares it as an internal key: a stored row is skipped quietly on load, and no API, page or export can read, set or list it.

## [0.3.0rc25] - 2026-11-20

### Added

- `spark bootstrap` can add an Amazon Bedrock connection that uses the task's IAM role: set `bootstrap.bedrock.region` (`SPARK__BOOTSTRAP__BEDROCK__REGION`), and optionally `bootstrap.bedrock.name` and `bootstrap.bedrock.residency_regions` (the Regions the cross-Region AU inference profiles may be processed in; global is never allowed). The connection `aws-bedrock` is added once, never changes or re-adds one an administrator has edited, disabled or deleted, and is added on an instance bootstrapped before this setting existed.

## [0.3.0rc23] - 2026-11-20

### Fixed
- **Server start-up confirms its identity keys against the secrets store, not konfig's cache.** The first full test deployment (0.3.0rc22) lost the TOTP and CSRF key writes to a stale read on Secrets Manager, and failed in warden's `SecretBox` with "secret warden.totp-key is not set", because Spark's own check after each write read konfig's write-through cache. `_ensure_secret` now refreshes konfig before reading back, and, before warden's `Identity` is built, start-up refreshes again and confirms the TOTP key, the CSRF key and the active signing key's seed by name; a missing one stops start-up with a `StartupConfigError` naming it (never its value). A signing key whose seed the store holds is kept on a re-run, otherwise a new one is rotated in, so `spark bootstrap` recovers the state rc22 left (logins, a signing key, no TOTP or CSRF key, no administrator). `cognisn-konfig` is now `>=0.5.1`, whose AWS bundle backend reads its own writes. `docs/deployment/aws.md` describes recovering a failed migrate task and confirming the keys by name

## [0.3.0rc22] - 2026-11-19

### Fixed
- **The deploy-test job copies the image to ECR with the digest unchanged.** The first real run (0.3.0rc21) used `docker buildx imagetools create`, which writes a new manifest, so ECR held a different digest from the published image and the job's own check stopped it before any AWS change. The copy is now `scripts/copy-image.sh`, which uses `crane copy` (a pinned, checksummed release installed by the job): it succeeds when ECR already holds the tag at the same digest, fails with a new-release-candidate message when it holds another digest (tags are immutable), and otherwise copies and requires the ECR digest to equal the published one. Stub tests cover each case and the masking of every identifier; `docs/deployment/aws.md` explains the copy. The next release candidate tag is the first real run of the fixed copy

## [0.3.0rc21] - 2026-11-19

### Changed
- **The live end-to-end stack comes up through `spark bootstrap`** (`deploy/live`, `tests/live`; test only). `stack.py prepare` writes the bootstrap settings (a synthetic first administrator at `example.com`) and the start-up mail settings into the sandbox's env file, keeping an existing file's konfig key and adding only what it lacks; `up` stores the SSO client secret and the mail password, lets the `migrate` service run `spark migrate && spark bootstrap` and starts the rest only once it succeeds, and no longer runs `spark identity create-admin`. The suite accepts the first administrator's invitation from the mail catcher's inbox on its first run against a stack. The overlay keeps `deploy/compose.yaml`'s shape and its one mail catcher, `mailpit`, now with STARTTLS from the stack's throwaway CA so `web2` and the workers, which keep network namespaces of their own, can send to it; the MCP sidecar joins the catcher's namespace, which `web` shares, and every published port stays on 127.0.0.1. Tests that create their own stub connection now declare the stub's model on it, as overlays belong to one connection since 0.3.0rc14
- **The live suite's runbook describes the bootstrap first run** (`deploy/live/RUNBOOK.md`): the settings `prepare` writes, what `up` does now, the stack's shape around the one mail catcher, how the first administrator is taken through bootstrap's invitation, the model stub's models declared after its connection is created, and the 0.3.0rc20 results (21 passed on a fresh stack, including the two tests that failed at rc9)

## [0.3.0rc20] - 2026-11-19

### Added
- **A release candidate deploys itself to the test environment.** A `deploy-test` job in `container-publish.yml` runs after the image is published for a `v0.3.0rcN` tag (never a final version or a manual dispatch), in the `spark-test` environment: it copies the image from ghcr.io to ECR by version, then `scripts/deploy-test.sh` sets `MigrateImageTag` through a limited CloudFormation change set, runs the one-off migrate task (`spark migrate && spark bootstrap`) and requires exit code 0, sets `ImageTag` through a second change set, and checks the stack, both services and `/ready`. Every AWS identifier is masked in the public run log. See [Running Spark on AWS](docs/deployment/aws.md#deploying-to-the-test-environment)

## [0.3.0rc19] - 2026-11-19

### Changed
- **The deployment guides describe `spark bootstrap` as the first run.** The deployment index, Docker Desktop and Portainer guides, the AWS guide's first-run lines, the README and the architecture notes now give `spark migrate` then `spark bootstrap` (with the bootstrap settings, the first administrator invited by mail and the mail catcher) as server mode's first run, and `spark identity create-admin` only as recovery, linking to the identity guide. `deploy/live/RUNBOOK.md` records what the live suite has run (the live provider step included) and what bootstrap changes for its stack
- **The Docker Desktop and Portainer stacks come up headless.** Their `migrate` service now runs `spark migrate && spark bootstrap`, web and worker start after it (the worker no longer waits for web), and a local mail catcher (`mailpit`) shares its network namespace with Spark's containers as in `deploy/compose.yaml`, so the first administrator's invitation arrives in its inbox with no `spark identity create-admin` step. The first administrator and the mail settings come from `SPARK__BOOTSTRAP__ADMIN__*` and `SPARK__MAIL__SMTP__*` variables (a placeholder administrator at `example.com` in the example environment); the web port is published by the catcher, and the Desktop `https` profile now proxies to it. Ports, volumes and image tags are unchanged. `tests/test_deploy_stacks.py` checks the new shape

## [0.3.0rc18] - 2026-11-19

### Changed
- Development: black is pinned at 26.10.1.

## [0.3.0rc17] - 2026-11-19

### Fixed
- **A model declared on a running connection can be used at once.** conduit's provider keeps the catalogue it was built with, and the connection runtime reused a provider whose settings, residency and secrets were unchanged, so a model declared through the models view (`POST /api/connections/{id}/overlays`) was offered but every turn on it failed (`model ... is not declared in the catalogue`) until a restart or a change to the connection. A digest of the connection's own overlay entries is now part of its provider's fingerprint: declaring a model, declaring it again or removing it rebuilds that connection's provider on every replica and worker that hears of the change, and every other connection keeps its provider. The live suite's set-up now declares its models after creating the stub connection, as an administrator would

## [0.3.0rc16] - 2026-11-19

### Added
- **A server comes up with nobody at a keyboard: `spark bootstrap`.** Run once as the schema owner after `spark migrate` (Compose's `migrate` service now runs `spark migrate && spark bootstrap`), it checks every bootstrap setting and fails once listing each missing or invalid one by its dotted and `SPARK__` names; creates the restricted database logins for web, worker and the audit purge from the URLs they connect with (`bootstrap.database.web_url`, `bootstrap.database.worker_url`, `worker.audit_purge_database_url`), with Spark's grants and warden's own, and verifies them; creates the instance identity (warden's signing key, the TOTP key, the CSRF key); stores the start-up `mail.smtp` values while the database holds none; and invites the first administrator (`bootstrap.admin.email`, `first_name`, `last_name`) through warden with the administrator role staged, by mail. It runs in one PostgreSQL advisory lock, records completion in a new `bootstrap` table (revision `bootstrap_record`), and a later run sends nothing; `--resend-invitation` resends. A URL asking for `sslmode=verify-full` needs a readable root certificate, and the refusal names the setting, never the URL
- **Web and worker wait for bootstrap** instead of racing it or exiting: the worker waits for the instance identity, and both for the bootstrap record when a first administrator is configured, checking every `bootstrap.wait_interval_seconds` (5) for up to `bootstrap.wait_seconds` (600), with one log line, then exit naming what is missing. A restricted login that does not exist yet counts as waiting, and while web waits its port answers `GET /health` with 200 (and `/ready` with 503), so health checks keep passing
- **A local mail catcher in Compose** (`mailpit`, inbox on `127.0.0.1:8025`), and example bootstrap and mail settings in `deploy/spark.env.example`

### Changed
- **`spark identity create-admin` is for recovery**: a new server's first administrator is invited by `spark bootstrap`; the command still restores an administrator or links the interim user guid
- **Compose**: the worker no longer waits for web to be healthy, and the Spark services share the mail catcher's network namespace, so it publishes the web port. The restricted database roles are made by bootstrap rather than by hand (docs/deployment/database.md)

## [0.3.0rc15] - 2026-11-19

### Added
- **`GET /health` and `GET /ready`** (`spark.web.endpoints.health`), for load balancers and container health checks, in personal and server mode, with no sign-in and outside warden's checks. `/health` answers 200 whenever the process serves and depends on nothing. `/ready` answers 200 only when the database answers, the schema is at both Alembic heads (Spark's and warden's) and the secrets store is readable, otherwise 503; its body names each check and Spark's version and never a secret, URL, host name or stack trace.
- **Running on AWS** (`docs/deployment/aws.md`): ECS on Fargate task definitions for web, worker and the one-off migrate task, EFS access points, the konfig settings document and secrets bundle, a database URL per login through `SPARK__DATABASE__URL=secret://database_url_<login>`, TLS to RDS, load balancer health checks, Bedrock through the task role, and each task role's policy.
- **The AWS RDS certificate authority bundle in the image** at `/etc/ssl/certs/rds-global-bundle.pem` (public certificates only, fetched at build time and checked against a pinned SHA-256), so `sslmode=verify-full` works against RDS with no `sslrootcert` in the URL when a task sets `PGSSLROOTCERT` to that path. `deploy/check-image.sh` checks the file is present, readable by the image's user and holds no private key.
- Tests that run konfig's settings document and secrets bundle against a stubbed Secrets Manager (a server starts with no master key; `SPARK__DATABASE__URL` selects the owner, web or worker login for web, migrate and worker and wins over any other `database.url`; every settings document key is honoured with its precedence against `SPARK__` variables; `KONFIG_AWS_SEED=false` leaves a pre-filled bundle untouched), that every connection Spark opens honours `PGSSLROOTCERT` and never falls back to a weaker TLS mode, and that a Bedrock connection with `credential_chain` builds its client with no keys.

### Changed
- The image's health check requests `/health` instead of `/loading/api/status`, which is unchanged.

## [0.3.0rc14] - 2026-11-19

### Fixed
- **A model declared on one connection is offered on that connection only.** Catalogue overlay entries (a Foundry deployment, a model pulled into Ollama) belong to the connection that declared them, and each connection's catalogue is conduit's catalogue plus its own entries. Before, a declaration was offered on every connection of the same catalogue, such as a direct Anthropic connection beside a Foundry one, or a second Ollama. The 409 for an id conduit's catalogue already lists, and declaring an entry again to correct it, now apply per connection; two connections may declare the same id with their own Region and limits. Deleting a connection deletes its declarations

### Changed
- **Overlay entries are kept per connection**: on a server, `catalogue_overlays` gains a `connection_id` (new revision `overlay_connection_scope`, with a downgrade that refuses while two connections declare one model differently); existing entries are copied to every connection that offered them, so none loses a model. In personal mode they live under each connection's `overlays` in `config.yaml`, and entries in the database from earlier 0.3.0 builds move there on the next start

## [0.3.0rc13] - 2026-11-19

### Fixed
- **The chat stream always tells the page a compaction ran.** After a turn finished, the stream forwarded only the tool events still queued, so the `compaction_start` and `compaction_complete` events the compactor reports at the end of a turn were often dropped and the page never showed "Compaction complete". Every event still queued when the turn ends, of every kind, is now sent in order with the same mapping as during the turn, before the final response

## [0.3.0rc12] - 2026-11-19

### Added
- **Declare a model by its id** in the models view of any connection (the administrator's Connections page, or Settings in personal mode): type the model id with its catalogue fields (kind, context window, output limit, capabilities, residency scope and Regions), without discovery. This is how a Microsoft Foundry deployment is added, by its deployment name; a Foundry connection offers only the deployments declared for it. The declaration uses the same overlay API, validation, CSRF check, permission and audit as declaring a discovered model

### Fixed
- **A Microsoft Foundry connection can offer models from the screens.** Foundry discovery is unsupported and the default in-Region policy refuses every public catalogue model, so until now a deployment could be declared only through the API
- **Declaring a model refuses an id conduit's catalogue already lists for the service** (409, naming the model). Before, such a declaration silently replaced conduit's entry for every connection of that service; Spark's own declarations can still be declared again to correct them

## [0.3.0rc11] - 2026-11-19

### Changed
- **The live end-to-end suite runs on connections and credits** (`tests/live`, `deploy/live`; integration-marked, never in the default suite): the rc1 checklist now chats through a `stub-ollama` connection on conduit's Ollama adapter, checks a connection's key changed on one replica against the other, and stops the send route at 100% of a role's credit allowance. New checks cover two connections of one service and one of another with discovery, an overlay declaration and a residency refusal; assignment and the allowance that applies; the 80% warning, the 100% stop with a turn in flight finishing and the usage CSV; MCP over HTTP with its audit event; a skill loaded with `use_skill`; a forced compaction; and personal mode's one-time upgrade of a 0.2 `config.yaml`. The scripted model server reports Ollama token counts and lists models over https for an Anthropic connection, an MCP server joins the stack, and the live chat uses a connection whose key is stored straight into the stack's secrets store. `deploy/live/RUNBOOK.md` is the runbook

## [0.3.0rc10] - 2026-11-18

### Changed
- **The wave gate**: the full suite now runs locally on the release branch tip at the end of each wave (`bash scripts/wave-gate.sh`), and the CI workflow (`ci.yml`) runs only when started by hand (`workflow_dispatch`), no longer on every push and pull request. The gate covers black and isort at their pinned versions, the blocking strict mypy list, the licence audit, the SQLite suite, the PostgreSQL suites (testcontainers), the uv smoke test, and the container image build with its checks. The macOS and Windows runs, Python 3.13 and the live end-to-end suites are not part of it. Publishing on a version tag is unchanged

### Added
- `scripts/wave-gate.sh`, the full suite command for the wave gate

## [0.3.0rc9] - 2026-11-18

### Changed
- **Connections guide** (`docs/connections.md`): adding a connection for each of the nine services, with its fields and authentication modes, the Region and residency policy and their defaults, several connections of one service, model discovery, the models view (enabling models, tiers, declaring a model the catalogue lacks), managing connections, personal mode, and upgrading from 0.2 (`config.yaml` providers converted on first start; `providers.*`, `models.offered` and `context_limits` no longer read)
- **Metering guide** (`docs/metering.md`): metered and unmetered connections, the base rate, tier multipliers and token weights (defaults 1,000, 2 and 0.5, and 1, 5, 0.1 and 1.25) with a worked example from `spark.usage.credits.estimate`, precision, assignments and allowances (a user's own over their roles', the highest role), the 80% warning, the 100% stop, the monthly reset, the usage view and its CSV, and the end of token budgets
- **Model services** (`docs/providers.md`) now covers getting an account and a credential from each service, linking to conduit's providers guide for how each is called; the 0.2 provider settings, model tables and `context_limits` are gone
- **In-app help and setup guides describe connections**: the user guide's Getting Started and Connections topics (in place of LLM Providers and its 0.2 model lists) and each provider setup guide's Spark steps now add a connection and choose its models, with credits on a server; an unknown setup guide redirects to the connections screen (`/admin/connections` on a server, `/settings#section-connections` in personal mode) in place of the removed providers section. The default `config.yaml` comments and examples use connection secrets and `connection/model` references, and say no code reads `token_management`
- **README**, getting started, configuration, security, architecture, development, containers and the server deployment pages describe connections, credits and allowances in place of providers and token budgets; the MCP guide gives the rule that plain `http` reaches loopback MCP servers only, conduit's failure classes and `secret://` resolution; the architecture describes MCP on the conduit loop (no MCP loop thread); conversations and tools describe compaction and skills on conduit; identity describes warden 0.1.0's password policy and forced password change

## [0.3.0rc8] - 2026-11-18

### Changed
- **MCP servers are reached through conduit's MCP client** (`cognisn-conduit[mcp]`, over the official MCP SDK): stdio (personal mode only, still refused on a server), streamable HTTP and SSE, with conduit's connection lifecycle, timeouts and clean shutdown. SPARK keeps its own policy above it: which servers a user may use (`spark.mcp.<server>`, now decided in the tool registry with the built-in tools), the audit event for every tool call, approvals, and the untrusted-content framing and inspection of every result, which happens once, in SPARK
- **MCP sessions run on the process's conduit loop**, the one the model connections use; the separate MCP loop thread is gone. The MCP settings endpoints run their connections there too, so a server saved or tested in personal mode no longer connects on the request's loop
- **MCP credentials reach conduit only as `secret://` references**, resolved through SPARK's resolver over konfig. A `secret://` value in a personal `config.yaml` MCP entry (a token or a stdio environment variable) is now resolved from the secrets store instead of being sent as written. Bearer tokens, API keys, basic credentials and custom headers all still work
- **Plain `http` MCP servers are accepted on this machine only** (`127.0.0.1`, `::1` or `localhost`); any other MCP server needs `https`. On a server an entry with a plain `http` URL to another host is refused when saved and ignored, with a warning, if already stored
- A server that cannot be reached at start-up is logged by name and left disconnected while the others connect; it is tried again when its settings change
- An MCP tool failure is reported with conduit's failure class (timeout, closed, rejected or protocol) and never the server's own error text
- **cognisn-conduit 0.1.0rc4 or later** is required, with its `mcp` extra (conduit 0.1.0rc4 accepts `mcp>=2.3,<3`)

## [0.3.0rc7] - 2026-11-18

### Added
- **Connections screens**: administrators manage model connections on a new Connections page (`/admin/connections`, server mode; in the Account menu and on the dashboard), and personal mode in a Connections section at the top of Settings. The list shows each connection's name, service, Region, residency policy, enabled models, last model discovery and status, and in server mode whether it is metered and how many assignments it has; a connection can be enabled, disabled, renamed and deleted. Deleting asks to confirm, and is refused while a scheduled action uses the connection, naming the actions
- **Add connection**: a drop-down of the nine services (Amazon Bedrock, Claude through Amazon Bedrock (mantle), Anthropic, Claude in Microsoft Foundry, OpenAI, X.AI, Microsoft Foundry (OpenAI-compatible), Google Gemini, Ollama) opens a modal with that service's fields from conduit's settings for its kind, an authentication chooser where it has several (long-term Bedrock API keys only once their allow setting is ticked), the Region and the residency policy (each service's default, or chosen scopes and Regions). Saving validates through conduit's provider registry and shows its refusal. Edit uses the same modal. Several connections of one service may exist
- **Write-only connection credentials**: a credential typed into the modal goes straight to the secrets store as `connections.<id>.<field>` (other server replicas are told to refresh), and the connection keeps only its `secret://` reference. Pages see each credential only as set or not set; a blank field keeps the stored one, and a credential no longer used (after a change of authentication, or a delete) is removed
- **Models view per connection**: conduit's catalogue models (with SPARK's own declarations) for the connection's service and Region, each marked reachable or not by the last model discovery (run on demand, and after each save, through conduit; the time of the last run is shown), refused with conduit's reason where the residency policy forbids it, with an enabled switch and a tier (lower, standard, higher). The first change keeps every model the connection offered until then. A reachable model the catalogue lacks (an Ollama model, say) is listed apart with a form declaring it to SPARK's catalogue (kind, context window, output limit, tools, vision, prompt caching, structured output, residency scope and Regions)
- **Metering and assignment on the connection** (server mode): metered or not, the base rate, the higher and lower tier multipliers and the four token weights, with a reset to the defaults and a worked example of a sample turn's credits at each tier; assignment to roles and named users, each with a monthly credit allowance. On a metered connection an allowance must be given, or No limit chosen explicitly
- **Permission `spark.admin.connections`** (server mode), held by the Administrator role: it guards every connections page and API (403 without it), and it is what lets a user see and try a connection that is not yet assigned to anyone
- Every connection change is recorded in the audit trail under the instance settings actions, naming the fields changed and never their values

### Changed
- **cognisn-conduit 0.1.0rc3 or later** is required (model discovery). Its `mcp` extra is not taken yet: conduit 0.1.0rc3 still asks for `mcp<2`, which SPARK's `mcp>=2.3` excludes
- **Administrators of connections**: a connection assigned to no one is offered to holders of `spark.admin.connections`, in place of `spark.settings.instance`
- The provider setup guides link back to the connections screens

## [0.3.0rc6] - 2026-11-16

### Added
- **Credits** (`spark.usage.credits`): every model call on a connection is charged credits when it is recorded, as (input x input weight + output x output weight + cache read x cache read weight + cache write x cache write weight) / the connection's base rate x the model's tier multiplier (standard 1, higher and lower set per connection; weights 1, 5, 0.1 and 1.25 by default). Credits are exact to six decimal places, rounded half up once per call, and each usage row keeps the rates it was charged at, so changing a connection's rates never changes what earlier calls cost. `credits.estimate(connection, counts)` gives what sample token counts would cost at each tier
- **Monthly credit allowances per metered connection** (server mode): a user's allowance on a metered connection is their own assignment's, otherwise the highest of their roles' assignments. Users are warned at 80%; at 100% new requests on that connection (chat turns on the stream and the send route, debate and panel turns and questions, scheduled actions and knowledge graph build chunks) are refused with a message naming the connection and the reset date, while a request already running finishes. Allowances reset at 00:00 UTC on the first of each month. Unmetered connections, and personal mode, have no limit
- **Knowledge graph builds are metered**: every extraction call is billed to the build's user (usage source `knowledge`)
- **The model picker** groups models by connection, shows each model's tier and, on a metered connection, the credits left this month, in a chat and when starting a conversation, debate or panel
- **`/api/usage/me`** answers in credits, for one connection (`?connection=` or `?model=`) or the user's most used metered connection, with every metered connection of theirs under `connections`

### Changed
- **The usage view and its CSV export** (`/admin/usage`) show usage per user, connection and model with the four token counts (input, output, cache read, cache write) and credits, totals per connection, and each user's standing on each metered connection
- **The credit banner** on the chat, debate and panel pages speaks of credits on a named connection instead of a token budget

### Removed
- **A14's token budgets**: role token budgets, per-user token budget overrides and their forms on the usage view (`/admin/usage/budgets/roles` and `/admin/usage/budgets/users`). Revision `credit_metering` drops `usage_role_budgets` and `usage_user_budgets` and adds `credits_micro` and `credit_inputs` to `usage_tracking`

### Upgrade notes
- **Server mode**: 0.3.0 was never released, so token budgets set on a release candidate are not migrated to credit allowances. Set allowances on each metered connection's assignments instead. Usage recorded before this revision costs no credits

## [0.3.0rc5] - 2026-11-16

### Changed
- **Skills on conduit**: SPARK's bundled and user skills are listed and loaded through conduit's skill layer (`spark.skills.source.SparkSkillSource` over conduit's filesystem source), keeping the rule that a bundled skill wins its name and serving each user only the skills they have turned on, globally and for the conversation. In chat, `use_skill` is conduit's progressive disclosure: a loaded skill's instructions join the system prompt, before its cache breakpoint, instead of the tool result, and stay loaded in later turns of the conversation (restored in order from its stored messages, across a compaction too), so the cached prefix is the same from call to call. `read_skill_resource` reads a skill's files once it is loaded. Debates, panels, sub-agents and scheduled actions keep receiving a skill's instructions as the tool result
- **SKILL.md validation is conduit's**: the skills page, import and skill authoring check every `SKILL.md` with conduit's parser, the one the model loads through. `version`, `license`, `compatibility`, `allowed-tools` (a list) and `metadata` are recorded; any other front matter key, a block value (`|` or `>`) or an empty body makes a skill invalid, with the reason shown. Authored skills write their description quoted, and their resource paths follow conduit's rule (letters, digits, `_`, `.` and `-` segments)
- **Compaction on conduit's compaction layer**: `ContextCompactor` keeps SPARK's summary prompt, markers and rollup history, and lets conduit's compactor decide and cut. The latest assistant turn now stays as it was after the summary, and the model reads the summary first; while a conversation still fits the model's window, a compaction happens only when it brings it under the threshold; past the emergency threshold without a summary, the older turns give way to the truncation notice and the latest turn stays. The system prompt and loaded skills are never touched, so the cached prefix is unchanged by a compaction
- **Compaction thresholds and prompt caching follow the instance settings at once** (server mode): `conversation.rollup_threshold`, `rollup_summary_ratio`, `emergency_rollup_threshold` and `prompt_caching` are read before each compaction check and model call, without a restart; a conversation's own prompt caching choice still applies

### Upgrade notes
- **Skills**: a user skill whose `SKILL.md` uses front matter keys other than conduit's, or block values, shows as invalid on the skills page until it is edited

## [0.3.0rc4] - 2026-11-16

### Added
- **A forced password change at sign-in** (server mode): when warden requires a new password before it issues a session (an administrator asked for it, the password expired, it no longer meets the password policy, or it is on a watch list), the sign-in goes on to a new-password step on the challenge screen (`POST /auth/challenge/change`) after the password and any second factor, and signs in once warden accepts the new password. After a forced enrolment the recovery codes are shown first, then the new-password step. No session cookie is set until then

### Changed
- **warden 0.1.0**: SPARK depends on `cognisn-warden>=0.1.0`, warden's first release, in place of 0.1.0b8. `spark migrate` brings warden's chain from 0.1.0b8's head (`7b2e9d4c1f08`) to 0.1.0's (`a9e4c27f13d8`), adding warden's passkey and password policy tables. warden brings `fido2` (BSD-2-Clause) as a new runtime dependency
- **Password refusals**: a password warden's policy refuses, on the account page, an invitation or a reset, shows warden's plain messages (for example "Use at least 12 characters. This password is too common.") instead of an error code list or, on the account page, an error page

### Upgrade notes
- **Server mode**: warden 0.1.0 enforces its default password policy, which refuses about 100,000 very common passwords wherever a password is set. A user whose current password is on that list is asked to choose a new one at their next password sign-in

## [0.3.0rc3] - 2026-11-16

### Added
- **Named model connections on conduit** (`spark.connections`): every model is now reached through conduit (`cognisn-conduit` 0.1.0rc2 or later, with the extras for Amazon Bedrock, Claude through Bedrock's mantle endpoint, Anthropic and Claude in Microsoft Foundry, OpenAI, X.AI and Microsoft Foundry, Google Gemini and Ollama, and konfig). A connection is one named service with its own conduit settings, Region and residency policy (in-Region for Bedrock, mantle and Foundry, global for direct APIs, local for Ollama, by default); several of one kind sit side by side, and every credential is a `secret://` reference resolved through konfig only when the provider is built. One connection runtime per process owns conduit's loop thread and builds one provider per enabled connection through conduit's provider registry, rebuilding a connection live when its settings or secrets change and never dropping a working one for a change that fails
- **Connection schema and repositories** (revision `connections`): connections, the models each offers (enabled, and a tier of lower, standard or higher), SPARK's catalogue overlay entries, per-connection metering settings (base rate, tier multipliers, and token weights of 1, 5, 0.1 and 1.25 for input, output, cache read and cache write) and assignments to roles or users with a monthly credit allowance; `usage_tracking` gains the connection and the cache read and write tokens apart. Every write notifies the other web replicas. The downgrade removes it all
- **Usage from conduit's events**: SPARK's event sink records every attributed model call from conduit's `model_call_finished` event, with the user, connection, model, task and the four token counts apart

### Changed
- **A model is a (connection, model) pair**, kept as `connection/model`. Chat turns, sub-agents, compaction, debates, panels, scheduled actions, knowledge graph builds and the action creation assistant each take a service of their own for it from the process's connection runtime, through conduit's sync facade; context windows and output limits come from conduit's catalogue plus SPARK's overlay. A model stored before connections (a bare model id) resolves to a connection that offers it
- **Models offered per connection**: a connection offers its enabled models, or, until some are chosen, every chat model the catalogue declares for its kind within its residency policy. On a server a user sees and may use only the connections assigned to them or their roles (a connection with no assignment yet is visible to administrators only); the model lists, every point where a model is chosen, each chat turn (403 on the send route) and each scheduled action's owner are held to it, and a sub-agent stays on its conversation's connection
- **Budget hook**: `spark.usage.budgets.check` and `status_for` take the connection a request will use; token budgets still apply across every connection. A user's monthly total counts uncached input, output, cache read and cache write tokens
- **The dashboard** lists the model connections the signed-in user may use, with their model counts
- **warden held at 0.1.0b8**, the build the 0.3.0 release line was proven against (warden 0.1.0b18 and 0.1.0 change its migration head and its typed session results)

### Removed
- **The 0.2 model layer** (`spark.llm`: the Anthropic, Bedrock, Gemini, Ollama and X.AI providers, `LLMManager`, its factory and `ContextLimitResolver`), and the direct `anthropic`, `boto3`, `botocore`, `google-genai`, `ollama` and `openai` dependencies (conduit's extras bring what its adapters need)
- **The providers settings**: the `providers.*` instance settings and `models.offered` (server), the LLM Providers section of the settings page, and `config.yaml`'s `providers` and `context_limits` sections. The connections screens arrive in a later 0.3.0 build

### Upgrade notes
- **Personal mode**: on the first start after upgrading, each provider in `config.yaml`'s `providers` section that is enabled or has a key becomes a named connection in a new `connections` section (`anthropic`, `aws-bedrock`, `ollama`, `google-gemini`, `xai`), keeping its keychain secret; a key still held in plain text moves into the keychain first. The `providers` section is removed, and the conversion never runs again. Bedrock connections start in-Region for their Region, so a cross-Region or global inference profile is refused until the connection's residency allows it. A conversation on a model conduit's catalogue does not declare answers with a message saying so: choose a model from the list
- **Server mode**: 0.3.0 was never released, so stored `providers.*` and `models.offered` settings are not migrated. They are ignored, and start-up lists them once; enter the services again as connections

## [0.3.0rc2] - 2026-11-05

### Fixed
- **Signing in from a browser in server mode**: the sign-in and account screens send no referrer, so browsers post their forms with `Origin: null`, which the Origin check refused for every browser sign-in. A `null` Origin now counts as no Origin, and the request passes only when the browser's fetch metadata says it came from the same origin (`Sec-Fetch-Site: same-origin`) and the Host is the public address; the screens keep `no-referrer`, so a link's token never leaves in a Referer
- **Docker Desktop guide**: the troubleshooting entry for the Origin refusal points to the web log's `serving` line and explains that changing the env file needs the `up -d` command again, not a restart

## [0.3.0rc1] - 2026-11-05

### Added
- **Docker Desktop stack** (`deploy/docker-desktop/compose.yaml`, `docs/deployment/docker-desktop.md`): server mode on one computer with every port on `127.0.0.1`, on `http://localhost` by default or behind Caddy on `https://localhost:8443` with the `https` profile, every credential a required variable from an env file, with the first administrator, backups that work from PowerShell, upgrades and troubleshooting
- **Portainer stack** (`deploy/portainer/stack.yaml`, `docs/deployment/portainer.md`): the server as a Portainer stack for a standalone Docker environment, configured only through the stack's environment variables (from the web editor or the Git repository), with a proxy container on the stack's network, the first administrator from the container console, standby workers, backups and upgrades

## [0.3.0a23] - 2026-11-05

### Fixed
- **Budgets on the plain send route**: `POST /chat/{id}/api/send` now gives the turn the signed-in user's roles, as the stream does, so a role budget stops it; a turn refused at 100% of the user's budget is answered 429 with the message and the user's standing instead of 500, and the model is not called
- **Live SSO test docstring**: the sample mock-oauth2-server configuration maps its token claims on `grant_type`, which the code exchange carries, and points to `deploy/live/stack.py` for the working configuration

## [0.3.0a22] - 2026-11-05

### Fixed
- **On/off settings from the environment**: konfig gives a `SPARK__` override as the string it was set to, so `SPARK__INTERFACE__BROWSER_HEARTBEAT__ENABLED=false` (set by the container image) read as on, and each web container stopped itself about every two minutes when no browser sent a heartbeat. Every on/off setting read from konfig now goes through one accessor (`spark.settings_values.setting_bool`) that takes `true`/`false`, `1`/`0`, `yes`/`no` and `on`/`off` in any case and refuses anything else, naming the setting: the browser heartbeat and auto-open, TLS, the LLM providers, autonomous actions and the daemon, prompt inspection, prompt caching, the voice cache, the email tool's TLS, and the switches shown on the settings and main menu pages
- **No browser heartbeat on a server**: the heartbeat monitor never starts in server mode, whatever `interface.browser_heartbeat.enabled` says
- **Live end-to-end overlay**: drops the day-long heartbeat interval it set to keep the web replicas running

## [0.3.0a21] - 2026-11-05

### Added
- **A separate connection for the audit purge** (server mode, `worker.audit_purge_database_url`): when set, to a PostgreSQL URL or a `secret://` reference resolved through the secrets store as `database.url` may be, the worker's `warden-audit-purge` duty connects with it for the purge alone, as warden's purger role, so the worker's own role can stay append-only on `warden.audit_events`. Unset, the purge uses `database.url` as before. A value that is not a PostgreSQL URL, or names a secret that is not set, stops `spark worker` at start-up with a message naming the setting, never the URL or its password

### Changed
- **Server deployment guide: database roles**: `docs/deployment/database.md` drops the `GRANT CREATE ON DATABASE` that web and worker roles needed, and says how to take it back; with an audit retention, a third login role, `spark_purger`, gets warden's purger grants (`warden audit grants --purger-role`) and the worker's env file names it in `SPARK__WORKER__AUDIT_PURGE_DATABASE_URL`, replacing the `DELETE` grant to the worker's role. The privilege checks cover `CREATE` on the database and the purger. The Compose guide, the operations page, the guide's index and a comment in `deploy/compose.yaml` follow

### Fixed
- **Server start-up as a restricted database role**: web and worker check the schema at start-up without running `CREATE SCHEMA IF NOT EXISTS spark`, which PostgreSQL refuses to a role without `CREATE` on the database even when the schema exists. Only `spark migrate` creates the schema, so web and worker can connect as login roles that own nothing and hold no `CREATE` on the database; an empty database is refused with "run `spark migrate`" as before. Personal mode is unchanged

## [0.3.0a20] - 2026-11-04

### Added
- **Server deployment guide** (`docs/deployment/`): running a Spark server with the Compose stack (services, roles, volumes, web and worker replicas, the migrate step, the files volume, pgvector 0.8 or later), konfig secrets in the encrypted store with `KONFIG_MASTER_KEY` or in AWS Secrets Manager with its IAM policy, and how a saved secret reaches every replica; the reverse proxy and TLS (`server.public_base_url`, the Origin and Host checks and the cookies), with Caddy and nginx examples; the first administrator (`spark identity create-admin`), roles, two-step verification, single sign-on through warden's settings and identity mail (`mail.smtp`, and links shown to administrators while it is unset); an external PostgreSQL, restricted database roles and the `DELETE` grant warden's audit purge needs on `warden.audit_events`; backups, restores, upgrades with `spark migrate` and rolling back; and moving people from personal mode with a user bundle. Each page says which of its commands were run for the guide and which were not
- **README: server mode**: a short section on running Spark as a multi-user server, linking the guide
- **Documentation index**: links the server deployment guide, and the container guide points to it instead of promising it for a later build

## [0.3.0a19] - 2026-11-04

### Added
- **Live end-to-end suite for rc1** (`tests/live`, `deploy/live`): the A18 checklist run against the Compose stack in server mode, marked `integration` and skipped unless `SPARK_LIVE_STACK` names a prepared stack, so CI and a plain `pytest` never run it. `deploy/live/stack.py` layers an overlay on `deploy/compose.yaml` with a second web replica, a standby worker, mock-oauth2-server and Mailpit behind a throwaway CA, and a scripted model server, writes the CA, certificates, `-dev-only` credentials and konfig key into a sandbox outside the repository, starts the stack and creates the first administrator with `spark identity create-admin`. The checks cover single sign-on, invitations and password resets mailed and completed from the link, a tool category a User may not have, the budget stop with the turn in flight finishing, a personal bundle exported and imported for a user, an untrusted tool result withheld, a chat attachment kept to its owner, a knowledge-graph build on the worker, worker failover, and a provider key saved on one replica used by the other without a restart. One live chat runs only when `SPARK_LIVE_PROVIDER` names the provider whose key the operator stored with `stack.py secret`

## [0.3.0a18] - 2026-11-04

### Changed
- **Container guide: identity mail**: `docs/container.md` describes setting `mail.smtp` on the instance page and the `mail_smtp_password` secret, and that invitations and password resets are mailed once a mail host is set
- **Reading your own files does not ask first**: `list_my_files` and `read_my_file` no longer prompt for approval in a conversation, as skill loading does not; they read only the user's own file area, and a server still refuses them to a role without the documents tools
- **Dependencies**: cognisn-konfig 0.5.0 or later, whose encrypted store reloads and locks, so a secret saved on one web replica reaches the others; the MCP SDK is pinned to its 2.x line (`mcp>=2.3,<3`), the major version the MCP client was rebuilt for
- **Strict typing in CI for the 0.3.0 server features**: the usage, bundles, MCP integration, chat attachment and knowledge graph job modules, with their endpoints and the model client factory, join CI's blocking `mypy --strict` step, replacing the three tests that ran mypy over them in the meantime

## [0.3.0a17] - 2026-11-04

### Added
- **Secrets reach every web replica** (server mode): saving or clearing a secret on the instance settings page sends a settings notification whose payload is the secret's key, never its value, and every replica's settings listener has konfig refresh its secrets store (the shared encrypted file or the AWS Secrets Manager bundle cache) before reloading, so the providers, voice and web search keys follow without a restart. The store is also refreshed whenever the listener reconnects. Two replicas saving different secrets at once both keep their change, through konfig's locked writes. Needs cognisn-konfig 0.5.0rc1 or later; with an older konfig a warning says secrets saved elsewhere apply only after a restart. The container guide's restart note is replaced

## [0.3.0a16] - 2026-11-04

### Added
- **Mail for invitations and password resets** (server mode, `mail.smtp`): administrators set an SMTP server on the instance page (host, port, TLS as STARTTLS, implicit TLS or none, sender address and name, username) with its password kept only in the secrets store (`mail_smtp_password`). Once a host is set, warden's invitations, resends, password reset links and second factor notices are mailed on a worker thread within 30 seconds, an administrator's screen says the link was sent instead of showing it, and a signed-out reset request is mailed while keeping its neutral answer. TLS is required and the server's certificate verified, except for plain text to a loopback relay; the instance page refuses plain text to any other host, and a host without a sender address. A message that cannot be delivered is reported without the credential or the server's reply. While the host is empty, links are shown to administrators once as before. Changes apply to the next message on every replica, without a restart. Personal mode has no mail settings
- **Test message** (server mode): an administrator sends a test message to their own address from the instance page (`POST /settings/api/mail/test`); a failure says why (for example the server refused the username or password) without echoing the credential. Each test is audited as `spark.mail.test_sent`, with its outcome; settings and password changes are audited by key, as other instance settings are

## [0.3.0a15] - 2026-11-04

### Added
- **Reading your own files** (both modes): two tools in the documents category, `list_my_files` and `read_my_file`, list and read the signed-in user's own file area: files attached in chat and documents Spark created. A file is addressed by its id; Word, Excel, PowerPoint and PDF files are read as text, other text files in pages of characters, and binary files are refused. Another user's file id answers as an id that does not exist. Personal mode reads the personal area (`<data directory>/files`) without any allowed paths configured; a server offers the tools only to a user holding `spark.tools.documents`. What they return enters the prompt as untrusted document content
- **Attachments in a chat message** (both modes): both send routes take the ids of files the user has uploaded to their area (`attachments`, a comma-separated query value on `/stream/chat`, a list on `/chat/<id>/api/send`). Each must be the signed-in user's own file, or the message is refused with 404 and nothing runs. The message the model receives then ends with a note naming each file by id and name, `[Attached files: read them with the read_my_file tool]`, never its content; the model reads the files with `read_my_file`. A message may carry up to 20 attachments, and may be attachments alone
- **Attach control in the chat composer** (both modes): a paperclip button uploads the chosen files to the user's own file area with the conversation id, through the page's CSRF-protected requests, and holds them as chips above the composer until the message is sent (each removable). Sent messages, live and from history, show their attachments as chips that download the file. A file over the upload limit is refused before it is sent, and refusals are shown above the composer naming the file: over the upload limit (with the limit), storage full (with the usage and quota), storage not available, or a refused upload. Personal mode attaches into the personal area

### Changed
- **The non-streaming send route follows the signed-in user's role** (server mode): `/chat/<id>/api/send` passes the user's permissions to the turn, as the streaming route does, so its tools follow the user's roles instead of being refused
- **Documentation**: the API reference gives the `attachments` parameter of both send routes and the note the model receives, and the architecture guide describes the attach flow and the two file tools

## [0.3.0a14] - 2026-11-04

### Added
- **User bundle format, version 1** (`spark.bundles`): one zip archive holding a user's conversations, memories, skills, knowledge graphs, scheduled actions and preferences, as JSON Lines per table plus file contents and skill files, under a manifest with the format version, a bundle id and each member's size and sha256. Only listed tables and columns are ever written, never a user guid or any instance configuration. Opening a bundle refuses one over the size limit, too many members or too large when unpacked, a member expanding too far, a member named outside the layout (absolute or parent paths, backslashes, hidden or device names), a link or encrypted member, a repeated member, another format or format version, a member that does not match the manifest, and anything shaped like a credential. Nothing is extracted to disk
- **Bundle export** (personal mode, `spark.bundles.exporter`): writes the local user's conversations (with their messages, files, links, tool and MCP toggles, debates, agent runs and context index), memories, knowledge graphs, skills (their folders and enable states), scheduled actions (with their tool permissions and run history), "Always" approvals, file area contents and preferences. Nothing of the instance travels: no provider, MCP server or other instance setting (only the transport of each MCP server the data refers to), no usage, voice usage, prompt inspection or daemon records, and only user-scope preferences. Every text value has the instance's secret values and anything shaped like a credential replaced, and a file or binary skill file holding one is left out and listed
- **Bundle import** (`spark.bundles.importer`): imports a checked bundle for one user in one transaction, every id mapped to a new one and every reference followed. Scheduled actions arrive disabled and unlocked (a clashing name gets " (imported)"). What a server cannot run is dropped and listed: approvals and per-conversation settings for the local-only tools and MCP tools, per-conversation MCP servers that are stdio or not offered by the server over HTTP or SSE, and the same in an action's tool permissions. The user's own memories, approvals, skill states and preferences win over the bundle's, and knowledge graph nodes and edges merge. Files land in the user's area under `imports/<bundle id>/`, charged to their quota; skills are kept there too, since a server's skills are instance-wide. On PostgreSQL imported vectors are added to pgvector. The report is kept as `imports/<bundle id>/report.json`, and importing the same bundle again for the same user is refused. A seeded personal SQLite database exported and imported into PostgreSQL is compared row for row in the tests
- **Export my data** (personal mode): a Settings button downloads the local user's bundle (`/bundles/export`, never cached), and `spark export [--output PATH]` writes it to a file (never overwriting one) and prints what it holds and left out. A server has no export
- **Import a user bundle** (server mode, `/admin/bundles`): an administrator uploads a bundle, chooses the warden user to import it for and sees the import report on screen. The page needs the new `spark.bundles.import` permission, which only the Administrator role holds, and is listed in the Account menu. The upload is cut off past 1 GB while it streams, and the bundle is checked before anything is written. The import and its audit record (`spark.bundle.imported`: the administrator, the target user, the bundle id and version and numbers of rows, files and items left out, never content) commit together; a refused import is audited as a failure with a reason code. Personal mode has no import page
- **User bundles guide** (`docs/bundles.md`): exporting, the version 1 format, what a bundle never holds, the import checks and what changes on the way in. A small synthetic version 1 bundle is committed as a test fixture and must keep importing

## [0.3.0a13] - 2026-11-04

### Added
- **MCP servers in server mode** (A12): a server allows HTTP and SSE MCP servers only. A stdio server runs a command on the host, so `mcp.servers` refuses one with a clear message however it is configured, and a stdio entry already stored is ignored with a warning while the other servers are kept. Server names are lower-case letters, digits, `-` and `_`, because each is part of its permission
- **MCP grants by role** (server mode): each server in `mcp.servers` lists the roles it is granted to (`roles`), which a user holds as the `spark.mcp.<server>` permission; a server listing no role is granted to no one. The MCP manager lists and calls only the servers a caller's permissions grant, and with no permissions lists nothing and refuses every call
- **MCP servers from the instance settings** (server mode, D37): the web process builds its MCP servers from the `mcp.servers` instance setting with credentials from konfig's secrets store, never from `config.yaml` (whose `mcp` section is ignored with a warning), and a change to a server or its credentials reconnects the affected servers on every replica without a restart
- **MCP editor on the instance page** (server mode): administrators add, edit, test, enable, disable and remove HTTP and SSE servers on the instance settings page, choose the roles each is granted to, and enter credentials write-only; the `/settings/mcp/*` API works on the instance setting and refuses stdio servers, unknown roles and invalid names with the reason
- **MCP servers shown per user** (server mode): the conversation tool panel, the main menu, the action tool list, the debate capabilities list and sub-agents show and use only the servers granted to the signed-in user's roles; requests made with an API key are granted no MCP server
- **MCP tools per user in a turn** (server mode): the tool list a chat turn sends to the model holds only the MCP servers granted to the turn's user, and each MCP call is checked against the same grants and audited with the turn's user and conversation; a sub-agent's MCP calls are audited in its parent conversation
- **MCP audit trail** (server mode): every MCP tool call is recorded in warden's audit trail with the user, conversation, server, tool and outcome (`spark.mcp.tool.called`), and every refusal as `spark.mcp.tool.refused`; arguments and results are never recorded

### Changed
- **MCP credentials** (server mode, A9): a server's bearer token, basic password and custom headers are not kept in `mcp.servers`; the setting refuses them, and a credential already stored there is dropped with a warning so it can be entered again into konfig's secrets store

### Fixed
- **MCP SDK 2.x**: HTTP MCP servers connect again (the SDK renamed its HTTP client), tool input schemas reach the model, a tool's own error is reported as an error, and an image result keeps its type. An unreachable HTTP server now fails to connect instead of cancelling the caller, and each connection is opened and closed in one task, as the SDK's transports require. MCP SDK 1.x still works

## [0.3.0a12] - 2026-11-04

### Added
- **Knowledge graph build jobs** (both engines): a new revision adds `kg_build_jobs`, one row per build, owned by its user, with its status (queued, running, complete, failed or cancelled), attempts, progress, result and error. A partial unique index allows one queued or running job per user and scope, so two web replicas cannot both start a build of the same graph. `spark.knowledge.jobs` enqueues, reads, cancels and retries a user's own jobs only; a worker claims the oldest queued job with `FOR UPDATE SKIP LOCKED` on PostgreSQL, so parallel workers never claim one job twice, and records progress and the outcome only while the job is still its own. The downgrade drops the table
- **Model clients without the web app** (`spark.llm.factory`): builds the enabled providers from a settings source with the web process's rules (Anthropic, AWS Bedrock, Ollama, Google Gemini, xAI), skipping a disabled or keyless provider and logging only the type of one that cannot start, and gives a model's own client (`ModelClients`), so the worker can call models without importing the web server
- **The worker builds queued knowledge graphs as their owner** (server mode): the active `spark worker` claims queued builds every `worker.knowledge_graph_poll_seconds` (default 5) and builds each as the user who queued it, reading and writing only that user's conversations and graph, with a model client from the instance settings as they are now. An owner with no warden user, a suspended or deactivated owner, or one without `spark.knowledge.instance` building the global graph is refused; the refusal is recorded on the job and audited (`spark.knowledge.build.refused`). A failed build is recorded with its error and can be retried; a cancel from any web replica stops the build before its next chunk, and a build left running by a stopped worker is failed after `worker.knowledge_graph_stale_seconds` (default 30 minutes) without progress, so it can be retried

### Changed
- **Knowledge graph builds are queued on a server** (server mode): starting a build queues a job for the worker instead of building in the web process (409 while one is queued or running for that graph), and the knowledge status, cancel and clear routes read the job table, so every web replica shows the same progress. `POST /knowledge/api/retry` queues a failed build again. Building, retrying or clearing the global graph needs `spark.knowledge.instance` (403 otherwise), and clearing waits until no build is active. The Knowledge page shows a queued build and offers Retry on a failed one. Personal mode still builds in-process

## [0.3.0a11] - 2026-11-04

### Added
- **Usage per model call and monthly token budgets** (both engines): a new revision gives every `usage_tracking` row the source of its model call (chat, compaction, agent, debate, panel or action), the action it ran for and its period, the UTC calendar month it was recorded in; existing rows take the month of their timestamp. Two new tables hold a monthly token budget per role and an optional per-user override, where an override may also be "unlimited". The usage repository records one row per call, sums a user's tokens for a month, reports usage by month, user and model, and sets and clears budgets. Downgrading removes the tables and columns
- **Usage metering and budget policy** (`spark.usage`): a metering wrapper records every model call a turn makes against the turn's user, with the model the call used and its source; input tokens include prompt-cache reads and writes, and a failure to record never breaks the call. The budget policy decides a user's monthly limit (their own override first, then on a server the most generous budget among their roles, otherwise unlimited), warns at 80%, refuses a new request at 100% with a message saying when the budget resets (00:00 UTC on the first of next month), and audits the refusal. Personal mode has no budget unless the local user's own budget is set. A new permission, `spark.usage.admin`, held only by Administrators, covers the usage view and budgets, and three audit actions record budget refusals, usage views and budget changes
- **Usage view, export and budgets** (`/admin/usage`, `spark.usage.admin`): an Administrator sees usage by month, user and model (users by email address on a server), each user's standing this month, and sets a monthly token budget per role and a per-user override (a number, `unlimited`, or empty to let the roles decide); `/admin/usage/export.csv` exports one month or every month, with cells a spreadsheet would run as formulas neutralised. Viewing, exporting and changing a budget are audited. Personal mode lists the view in its navigation and offers only the local user's own budget; a server lists it in the Account menu for Administrators only
- **Budget warning in the UI**: the chat, debate and panel pages show a banner from 80% of the user's monthly token budget, from `/api/usage/me` on load and from each finished turn, and say when it resets. A refused chat turn streams a `budget_exceeded` event with the message and stores nothing; a debate or panel run refused at the start, or paused part-way, says so and offers to resume; a question in QA past the budget is answered 429 without being stored

### Changed
- **Chat turns are metered and budgeted**: every model call a chat turn makes, its tool loop, its sub-agents and its context compaction (with a locked compaction model too) is recorded against the turn's user. A turn checks the user's monthly token budget before anything is stored: at 100% it is refused with a clear message, while a turn already running always finishes, even past the budget. The turn's result carries the user's budget standing for the UI's warning. The per-conversation token counters are unchanged
- **Debates and panels are metered and budgeted**: the judge's, moderator's, debaters' and panellists' model calls are recorded against the debate's or panel's user. A run or a question in QA is refused at 100% of the user's monthly token budget before anything starts; a run that reaches the budget part-way lets the speaker holding the floor finish, then pauses before the next turn with a `budget_exceeded` event, and resumes once the budget allows
- **Scheduled actions are metered and budgeted**: every model call of an action's run is recorded against its owner with the action. A run is refused at 100% of the owner's monthly token budget and the refusal is recorded in the action's run history, while a run already started finishes. On a server the worker reads the owner's roles from warden at run time with their permissions, and run now uses the signed-in user's roles

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
