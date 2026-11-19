# Runbook: the SPARK live end-to-end suite

The live suite drives a running server-mode stack over HTTP, as browsers on the public origin would. It is a test, never a deployment. Everything it generates (the throwaway CA, keys, `-dev-only` credentials, the konfig key, people and TOTP keys) lives in a sandbox folder outside the repository. Users, keys and data are synthetic (`example.com`).

## What it is

- `deploy/compose.yaml` in server mode, with the overlay `deploy/live/compose.live.yaml`. The base file's shape is kept: `migrate` runs `spark migrate && spark bootstrap`, and `migrate` and `web` share the network namespace of its mail catcher `mailpit`, which publishes web's port and its inbox. The overlay adds:
  - STARTTLS on `mailpit` (Mailpit v1.27, the base file's) with a certificate for `mailpit` from the throwaway CA. It is the stack's one mail catcher: every Spark container sends to `mailpit:1025` over verified TLS, whether or not it shares the namespace;
  - `web2`, a second replica on its own origin and its own network namespace;
  - two workers, one active and one on standby, each in its own network namespace (`network_mode: !reset null`), so the suite can tell them apart by address and host name;
  - `oidc`: mock-oauth2-server 6.1.3 with TLS from the throwaway CA;
  - `model-stub`: `model_stub.py`, a scripted Ollama API on http (11434) and Anthropic models listing on https (11443, the throwaway CA);
  - `mcp`: `mcp_server.py`, a streamable-HTTP MCP server in the `mailpit` namespace that `web` shares, on `127.0.0.1:8765`, so `web` reaches it on loopback (conduit allows plain http only there).
- The first administrator (`admin@example.com`, "Live Administrator") is invited by `spark bootstrap`; the suite accepts the invitation from the catcher's inbox on its first run against a stack (see "The first run" below).
- The public base URL is loopback http. The suite's client sends SPARK's `Secure` cookies itself.
- Every model call goes through conduit's own adapters on ordinary SPARK connections that name the stub (`http://model-stub:11434` for Ollama, `https://model-stub:11443` for Anthropic). SPARK's runtime does not offer conduit's in-process stub provider, so no connection uses it.
- Every test in `tests/live` is marked `integration` and skipped unless `SPARK_LIVE_STACK` is set. CI's `-m "not integration"` deselects them.

## The tests

- `test_rc1.py`: the A18 rc1 checklist (identity, SSO, a forbidden tool category, the send route's stop at 100% of a role's credit allowance, a personal bundle, untrusted content, the worker and its failover, an attachment kept to its owner, a knowledge-graph build, a connection key changed on one replica used by the other, identity mail).
- `test_conduit.py`: connections, discovery, overlays and residency; assignment and allowances; credits (the 80% warning, the stop at 100% with a turn in flight finishing, the usage CSV); MCP over HTTP with its audit event; a skill loaded with `use_skill`; a forced compaction; personal mode's one-time upgrade of a 0.2 `config.yaml` to connections.
- `test_live_provider.py`: one live chat through a connection to a real service, only on Matthew's word.

## The model stub's directives

Written in the user's message:

- `[[tokens N]]`: reply with N tokens (`abcd` N times), reported as `eval_count` N.
- `[[prompt N]]`: report `prompt_eval_count` N (default 10), so a turn's credits are predictable.
- `[[sleep S]]`: wait S seconds before replying (a turn kept in flight).
- `[[call NAME {json}]]`: call the tool NAME once; the next request, carrying its result, is answered `TOOL RESULT SEEN: <result>`.

`GET /_stub/requests` lists what was received (keys only as a SHA-256 prefix); `POST /_stub/reset` clears it. The stub lists `llama3-stub`, `tiny-stub` and `extra-stub`, none of which conduit's catalogue declares. The suite creates the `stub-ollama` connection first and then declares the first two as overlays on it (`tiny-stub` with a 4,096-token window, for compaction), as an administrator would; since 0.3.0rc17 a declaration rebuilds that connection's provider, so the models are usable at once.

## Before starting

1. Check that no other workstation is running a workstream of this release (Skuld `skuld_list_workstations`).
2. Check the heavy-workload cap: `docker compose ls` and `docker ps --filter label=org.testcontainers=true`. The stack counts as one heavy workload.
3. Activate the worktree's virtual environment and confirm `python -c "import spark; print(spark.__file__)"` points into the worktree.
4. Find your slot's port base in the sandbox's `ports.txt` (for example 22100 for slot 21).

## Ports (port base N, all on 127.0.0.1)

| Service | Port |
|---|---|
| web (published by `mailpit`) | N |
| web2 | N+1 |
| OIDC | N+10 |
| model stub (Ollama API) | N+11 |
| Mailpit inbox and API (published by `mailpit`) | N+25 |
| PostgreSQL | N+32 |
| personal-mode check (host process) | N+50 |

## Steps (from the worktree root, PowerShell)

```
docker build -t spark:<worktree> .
python deploy/live/stack.py --sandbox <sandbox> prepare --project spk-<worktree> --port-base N --image spark:<worktree>
python deploy/live/stack.py --sandbox <sandbox> up
$env:SPARK_LIVE_STACK = "<sandbox>\live\live.json"
python -m pytest tests/live -p no:cacheprovider --timeout=900 -rA
```

- **Build:** about a minute with a warm cache, about 6 minutes cold.
- **`prepare`:** writes the CA and certificates, the OIDC configuration, `live.env` (ports, image, paths) and the Spark services' `spark.env`: the database, the konfig key, SSO, the bootstrap settings and the start-up mail settings. It keeps an existing `spark.env` (above all its konfig key) and adds only the settings it lacks, so a sandbox from an older stack gains the bootstrap settings.
- **`up`:** starts `postgres`, `mailpit`, `oidc` and `model-stub`; writes the SSO client secret and the mail password into the konfig store; then `compose up -d`, where Compose runs `migrate` (`spark migrate && spark bootstrap`) and starts `web`, `web2` and the workers only once it has succeeded (a failed bootstrap fails `up` and prints the `migrate` log); then waits until both replicas report ready. It no longer runs `spark identity create-admin`. It is idempotent: bootstrap run again changes nothing and sends no second invitation.
- **The run:** 22 tests in about 5 minutes on a fresh stack (the personal-mode check starts SPARK twice on the host); `test_rc1.py` and `test_conduit.py` hold 21 of them, and the live chat (`test_live_provider.py`) is skipped unless `SPARK_LIVE_CONNECTION` is set. On 0.3.0rc20 all 21 pass (see below).
- **Known failures at rc9, since fixed:** `test_conduit.py::test_a_model_declared_on_a_running_connection_can_be_used_at_once` failed because a model declared on a running connection was offered but its provider still held the old catalogue (Skuld question 54; fixed in 0.3.0rc17, which rebuilds that connection's provider), and `test_conduit.py::test_the_stream_reports_a_compaction_after_the_reply` because the stream dropped the compaction status queued at the end of a turn (question 55; fixed in 0.3.0rc13). Both pass on 0.3.0rc20.

## What the live suite has run

### On 0.3.0rc20, the bootstrap first run (2026-11-19)

The image built from the worktree at 0.3.0rc20, the stack brought up with `stack.py up` on a fresh sandbox, and `python -m pytest tests/live/test_rc1.py tests/live/test_conduit.py -p no:cacheprovider --timeout=900 -rA`:

- `up` ran `spark migrate && spark bootstrap` in `migrate`: the identity keys were created, the start-up mail settings stored, and the first administrator's invitation sent to `mailpit` over STARTTLS. Both replicas reported ready; every published port was on 127.0.0.1.
- First run: 17 passed, 4 failed. The four failures (the send route's allowance stop, two connections of one service, assignment, credits) each created an Ollama connection of their own and expected the stub's model, declared on `stub-ollama`, to be offered on it. Since 0.3.0rc14 a declaration belongs to its connection (Skuld question 53), so each test now declares the model on the connection it creates (Skuld decision 126); the four then passed.
- Full run on a stack recreated from nothing (`down`, `up`): **21 passed, 0 failed, 0 skipped, in 311 s**, including the administrator taken through bootstrap's invitation, the two tests that failed at rc9, the worker failover and the MCP sidecar in the catcher's namespace.
- `up` run again on the running stack: bootstrap reported "already complete, nothing sent", and the catcher held one invitation to the administrator.

### On 0.3.0rc9 (2026-11-18)

All against the stub stack, with synthetic users and `example.com` addresses:

- The rc1 checklist (12 tests) passed, including the send route's stop at 100% of a role's credit allowance (429, no model call).
- Connections passed: two Ollama connections and one Anthropic connection added through `/api/connections`, discovery through conduit, an overlay declared and then offered, a second Ollama connection with its own `num_ctx`, and the Anthropic connection discovered over https behind the stack's CA (with a local-only residency policy every catalogue model was refused and none was offered).
- Assignment passed: a user without the role does not see the connection and gets 409 on a new conversation, and the limits resolve to the higher of two roles and a user's own over the role's.
- Credits passed: the picker's standing, the warning at 80%, a turn in flight finishing while another passes 100%, `budget_exceeded` with no model call, and the usage CSV row.
- MCP over streamable HTTP (a once-only permission, the answer reaching the model, `spark.mcp.tool.called` audited with no arguments), a skill loaded with `use_skill`, and a forced compaction passed.
- Personal mode's one-time upgrade of a 0.2 `config.yaml` to connections passed: the key moved to the keychain, the file and the log hold none, and the second start left the file unchanged.
- One live chat through a real service passed (Anthropic, `claude-haiku-5-5`, in 4 s, one usage row recorded). The first attempt failed because the key had not reached the store and discovery got 401; it passed after the key was stored again, and the connection, with its key, was deleted afterwards.

Not run: the live SSO test against a real provider (below), and the live chat on 0.3.0rc20.

## The first run (spark bootstrap)

`deploy/compose.yaml`'s `migrate` service runs `spark migrate && spark bootstrap`, which needs the bootstrap settings and a mail host and refuses to run without them (see [Identity](../../docs/deployment/identity.md#bootstrap)). `stack.py prepare` writes them into the sandbox's `spark.env`:

- `SPARK__BOOTSTRAP__ADMIN__EMAIL=admin@example.com`, `__FIRST_NAME=Live`, `__LAST_NAME=Administrator`;
- `SPARK__MAIL__SMTP__HOST=mailpit`, `__PORT=1025`, `__TLS=starttls`, `__SENDER_ADDRESS=spark@example.com`, `__SENDER_NAME`, `__USERNAME=spark`, with the password stored by `up` as the konfig secret `mail_smtp_password` (Mailpit accepts any sign-in). Bootstrap stores these as the `mail.smtp` instance settings while the database holds none.

No `bootstrap.database.*` URLs are set, so the stack keeps the one-owner arrangement.

Bootstrap then creates the instance's identity keys and mails the first administrator's invitation to `mailpit`; `web`, `web2` and the workers wait for it. On the suite's first run against a stack, the `admin` fixture finds that invitation in the catcher's inbox, accepts it with the password `prepare` chose (`admin_password` in `live.json`), enrols a TOTP key and keeps both in `people.json`; later runs sign in with them. `spark identity create-admin` is not run: it is the recovery path. If `people.json` is lost after the invitation was accepted, the administrator's TOTP key is lost with it: bring the stack down and up again. While the invitation is still pending, it can be sent again with `python deploy/live/stack.py --sandbox <sandbox> compose run --rm -T migrate "spark bootstrap --resend-invitation"`.

STARTTLS stays: SPARK sends plain text only to a loopback host, and `web2` and the workers do not share the catcher's namespace, so the catcher keeps a certificate from the throwaway CA (the `SSL_CERT_FILE` bundle every Spark container trusts) and is reached by its service name.

## Live provider (only when Matthew names the service)

All four steps were run on 2026-11-18 against the stub stack, on Matthew's word and with the key stored in his own terminal (Anthropic, `claude-haiku-5-5`; see "What the live suite has run"). Repeat them only when Matthew names a service.

1. Add the connection with a placeholder key: `python -m tests.live.live_connection --service anthropic` (or `gemini`, `openai`, `xai`). It prints the next command.
2. Matthew stores the real key in his own terminal. It needs a real TTY, so not Claude Code's `!`, and is never echoed: `python deploy/live/stack.py --sandbox <sandbox> secret connections.live.api_key --announce connections.live.api_key` (for `openai` and `xai` the name ends `service.api_key`).
3. Run it: `$env:SPARK_LIVE_CONNECTION = "live"` (`SPARK_LIVE_MODEL` optional), then `python -m pytest tests/live/test_live_provider.py -p no:cacheprovider -rA`.
4. Delete the connection afterwards (the Connections screen, or `DELETE /api/connections/live`), which removes its key from the store.

## Live SSO test (on Matthew's go-ahead)

Not run to date: it needs Matthew's go-ahead, and the scripted SSO sign-in in `test_rc1.py` passed against the same provider. With the stack up:

```
$env:SPARK_TEST_SSO_ISSUER = "https://127.0.0.1:<N+10>/default"
$env:SSL_CERT_FILE = "<sandbox>\live\certs\ca-bundle.pem"
$env:SPARK_TEST_POSTGRES_URL = "<postgres_url from live.json>"
$env:SPARK_TEST_POSTGRES = "required"
python -m pytest tests/test_identity_sso_live.py -p no:cacheprovider -rA
```

It uses a fresh database in the stack's PostgreSQL, so it adds no heavy workload. The mock maps claims on `grant_type`, as `stack.py` does.

## Teardown

`python deploy/live/stack.py --sandbox <sandbox> down` runs `compose down -v` for every file of the stack. Then run the sandbox's own `compose_down` from `ports.txt`, and check that `docker compose ls` no longer lists the project. Delete `<sandbox>\live` and `<sandbox>\personal` only at close-out, and prune the local image `spark:<worktree>`.

## Gotchas

- **TOTP:** warden accepts each 30-second step once. The client waits for a fresh step within one process, but two processes signing the administrator in within one step fail the second; wait 30 seconds between runs.
- **Overlays are per connection:** since 0.3.0rc14 an overlay belongs to the connection it was declared on (Q53), and since 0.3.0rc17 declaring one rebuilds that connection's provider (Q54), so the suite declares after creating.
- **Discovery results are per process:** a discovery run on `web` is not seen by `web2`.
- **MCP reaches only `web`:** the MCP server shares the `mailpit` namespace with `web`; `web2` and the workers cannot connect to it, and log that they cannot.
- **One namespace, many services:** `mailpit`, `migrate`, `web` and `mcp` share one network namespace (and so one address and host name). Anything that tells containers apart by address must not look at those; the workers have their own for that reason.
- **Password reset:** completing a reset ends the user's sessions; the suite signs the user in again.
- **Keep-alive:** uvicorn closes idle connections after 5 seconds, so the client disables keep-alive.
- **Logs:** `docker logs` writes to stderr, so read both streams.
- **The live overlay files** are excluded from `tests/test_deploy_compose.py`, which checks only `deploy/compose.yaml`.
