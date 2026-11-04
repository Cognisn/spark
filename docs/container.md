# Running Spark in containers

Spark ships one container image with two roles, plus a migration command:

| Command | Role |
|---|---|
| `spark web` (the image default) | The web interface. Plain `spark` is an alias. |
| `spark worker` | Background duties in server mode: scheduled actions, knowledge graph builds, and later releases' periodic jobs. |
| `spark migrate` | Brings the database to the current schema, then exits. |

The image is meant for **server mode** (PostgreSQL). This page covers building
the image, the roles, the Compose file in `deploy/` and upgrades. A full server
deployment guide (identity, TLS, backups) follows in a later 0.3.0 build.

## Build

```bash
docker build -t spark:dev .
```

The build has two stages:

- **builder**: a virtual environment with the CPU-only PyTorch wheel, then Spark
  and its dependencies. pip installs `torch` first from the PyTorch CPU index
  (`TORCH_INDEX_URL`), so installing Spark keeps it; the default Linux wheel would
  pull in the NVIDIA CUDA runtime. `scripts/check_no_nvidia.py` then fails the
  build if any `nvidia-*` package is installed, and the licence audit
  (`scripts/licence_audit.py`) runs against exactly this environment, so a
  dependency that breaks the licence policy fails the build. pip, setuptools and
  wheel are then removed.
- **runtime**: `python:3.12-slim` with `libmagic1`, the virtual environment
  copied in, no compiler and no pip. Spark runs as the unprivileged user
  `spark` (uid and gid 10001).

`.dockerignore` limits the build context to `pyproject.toml`, `README.md`,
`LICENSE`, `src/`, the licence audit and the NVIDIA check scripts: no tests, fixtures, local
databases, configuration or env files reach the image.

`deploy/check-image.sh IMAGE` checks a built image: it fails if the image runs as
root, carries a compiler or pip, has no healthcheck, contains tests, or if its
worker role does not refuse personal mode. CI runs it on every push and pull
request.

## Image layout

| Path | Purpose |
|---|---|
| `/var/lib/spark` (volume) | Configuration (`config/spark/config.yaml`), data, logs and konfig's encrypted secrets file (`data/spark/secrets.enc`). Share it between the web and worker containers. |
| `/var/lib/spark-files` (volume) | Users' file areas: uploads and documents Spark creates. Server mode needs `files.root` set to it (`SPARK__FILES__ROOT`). Share it between the web and worker containers. |
| `/var/cache/spark/huggingface` (volume) | The sentence-transformers embedding model, downloaded on first use. |
| `/opt/venv` | Spark and its dependencies. |

The image sets `SPARK__INTERFACE__HOST=0.0.0.0`, `SPARK__INTERFACE__PORT=8000`,
`SPARK__INTERFACE__OPEN_BROWSER=false` and turns off the browser heartbeat (which
would otherwise stop a web process no browser is attached to). The healthcheck
requests `/loading/api/status` on the web port.

## Configuration

Configuration comes through konfig, as on the desktop:

- **Environment**: any setting as `SPARK__<SECTION>__<KEY>`, for example
  `SPARK__MODE=server`, `SPARK__DATABASE__URL=postgresql://...`,
  `SPARK__LOGGING__LEVEL=DEBUG`.
- **Public address**: server mode refuses to start without
  `SPARK__SERVER__PUBLIC_BASE_URL`, the https address users reach Spark on (the
  TLS reverse proxy's host name, no path). Sign-in cookies are Secure and the
  Origin check accepts only this address, so users must reach Spark through it.
- **warden**: identity settings are the `warden` section, for example
  `SPARK__WARDEN__IDENTITY__REQUIRE_MFA=true` (the default) or
  `SPARK__WARDEN__AUDIT__TRUSTED_PROXIES=10.0.0.0/8` so warden believes the
  proxy's `X-Forwarded-For`. The web role creates warden's signing key and its
  TOTP and CSRF keys in the secrets file on first start.
- **File**: `config.yaml` in the data volume at
  `/var/lib/spark/config/spark/config.yaml` (created from the template on first
  start). Environment values override the file.
- **Secrets**: provider keys and other secrets entered in Spark are kept in
  konfig's encrypted secrets file in the data volume
  (`SPARK__SECRETS__BACKEND=encrypted_file`, at
  `SPARK__SECRETS__FILE_PATH=/var/lib/spark/data/spark/secrets.enc`), keyed by
  `KONFIG_MASTER_KEY`. Every web and worker container must read the same file,
  so the Compose file names it on the shared volume. When Spark is deployed into
  AWS, konfig can use AWS Secrets Manager instead (`KONFIG_AWS_SECRETS_MANAGER`).
  A secret saved on one web replica is used by that replica at once; other
  replicas read the file at start, so restart them after changing a secret.
- **Instance settings** (providers, models offered, tools, tool result actions)
  are kept in PostgreSQL. A change an administrator saves on one web replica
  reaches every replica within a few seconds, without a restart.

## Roles

### Web

`spark web` serves the interface on port 8000. Users sign in through warden with
their email address and password at `/auth/sign-in`; sessions live in warden's
tables, so any number of web replicas can serve one database without sticky
sessions. Create the first administrator once the stack is up:

```bash
docker compose -p spark -f deploy/compose.yaml run --rm -T web \
    identity create-admin --email admin@example.com < password.txt
```

(or without `-T` and the redirect, to be asked for the password twice). It is
safe to run again. `--link-user-guid GUID` gives the administrator the data made
by the interim single user before warden. A server no longer uses that interim
guid for anyone: every request acts for its signed-in user and every scheduled
action for its owner, so data made under it (scheduled actions included) belongs
to nobody until it is linked this way.

In server mode the web role does not start the tray daemon; scheduled actions run
in `spark worker`.

#### Users, two-step verification and single sign-on

- **First administrator**: run `identity create-admin` as above, then sign in at
  `/auth/sign-in`. With `warden.identity.require_mfa` on (the default) the first
  sign-in asks the administrator to set up an authenticator app (a QR code, or the
  key typed in) and shows ten recovery codes once: keep them somewhere safe.
- **Inviting users**: an administrator invites from **Account > Invitations**
  (`/admin/invitations`), choosing the roles granted on acceptance. Spark has no
  mail service in server mode, so the invitation link is shown to the
  administrator once, to pass on privately; it works once and expires after
  `warden.identity.invitation_ttl_seconds` (7 days by default). The same applies to
  a password reset link sent from a user's page. A user who asks for a reset at
  `/auth/password-reset` gets a neutral answer and no link, so they ask an
  administrator instead.
- **Two-step verification**: `SPARK__WARDEN__IDENTITY__REQUIRE_MFA` (default
  `true`) makes every password sign-in use an authenticator app. A user who has
  lost both their authenticator and their recovery codes needs an administrator
  to reset it from their user page (confirm who they are some other way first);
  they set it up again at their next sign-in.
- **Single sign-on** (OIDC, one provider): register
  `https://<public address>/identity/auth/sso/callback` as the redirect URI with
  the provider, then set the `warden.sso` section, for example
  `SPARK__WARDEN__SSO__ENABLED=true`, `SPARK__WARDEN__SSO__ISSUER_URL` and
  `SPARK__WARDEN__SSO__CLIENT_ID`, with the client secret in the secrets store
  under the name `warden.sso.client-secret`.
  Users must already exist (created or invited); the sign-in page then offers
  single sign-on, and an invitation can be accepted through it. A provider behind
  a private CA needs `SSL_CERT_FILE` naming a bundle that holds it and the public
  CAs.
- **Roles**: `administrator` (everything), `auditor` (the audit trail at `/audit`
  and its export, nothing else), `power_user` and `user`. Users create their own
  API keys at `/account/api-keys`, limited to their own permissions.
- **Usage and token budgets**: an administrator sees usage by month, user and
  model at **Account > Usage and budgets** (`/admin/usage`), exports it as CSV,
  and sets an optional monthly token budget per role and an own budget per user
  (a number, `unlimited`, or empty to let their roles decide). A user's own budget
  wins; otherwise the most generous budget among their roles applies. Users are
  warned from 80%; at 100% new chats, debate and panel turns and scheduled action
  runs are refused, while a request already running finishes. Budgets reset at
  00:00 UTC on the first of each month. The ElevenLabs character cap is separate
  and instance-wide.

### Worker

`spark worker` runs scheduled actions and knowledge graph builds. It refuses to
start in personal mode, on SQLite, or on a database behind the current schema.

The worker runs every user's enabled actions, each as its owner: the run, its
memories and the files it writes are the owner's, and its tools follow the
owner's roles at the time it runs. It starts warden over the same database and
secrets file as the web role, so it needs the same `server.public_base_url` and
`KONFIG_MASTER_KEY`. An action whose owner does not hold `spark.actions` (the
Power user and Administrator roles), is suspended or deactivated, or has no
warden user is not run: the refusal and its reason appear in the action's run
history, and the action runs again once its owner may run it.

Any number of workers may run against one database. One is **active**: it holds a
PostgreSQL advisory lock on a connection of its own. The others **stand by** and
retry every five seconds (`worker.standby_seconds`). When the active worker stops,
or its connection to the database is lost, PostgreSQL releases the lock and a
standby worker takes over. A worker that takes the lock waits about ten seconds
(twice `worker.check_seconds`) before it starts its duties, so a worker that has
just lost its connection has time to notice and stop first. Each scheduled action
also takes a lock in the database before it runs.

The worker logs `standing by` or `is active` when its state changes.

The active worker also prunes warden's expired data (refresh and single-use
tokens, second-factor challenges, sign-in throttle windows and single sign-on
records) every hour (`worker.identity_prune_seconds`). When
`warden.audit.retention_days` is set (365 or more), it also purges audit rows
older than that once a day (`worker.audit_purge_seconds`), behind a signed
checkpoint; the purge needs `DELETE` on `warden.audit_events`, so grant it to the
worker's database role or leave the retention unset to keep the trail for ever.

The active worker builds knowledge graphs. A build started on the Knowledge page
of any web replica is queued, and the worker picks it up within a few seconds
(`worker.knowledge_graph_poll_seconds`), builds it as the user who asked, with the
model clients the instance settings describe, and records its progress and outcome
for every replica's Knowledge page to show. The global graph needs the Power user
or Administrator role. A failed build can be retried from the page; a build left
running by a worker that stopped is failed after 30 minutes without progress
(`worker.knowledge_graph_stale_seconds`) so it can be retried. Without a running
worker, builds stay queued.

### Tray daemon

The system tray daemon is for personal mode only. In server mode it refuses to
start, with a message pointing to `spark worker`.

## Compose

`deploy/compose.yaml` runs the whole server stack:

| Service | What it does |
|---|---|
| `postgres` | `pgvector/pgvector:pg16` with the `postgres-data` volume. Not published. |
| `migrate` | Runs `spark migrate` once, after PostgreSQL is healthy. |
| `web` | `spark web`, after `migrate` succeeds. Published on `127.0.0.1:8000` by default. |
| `worker` | `spark worker`, after `migrate` succeeds and the web role is healthy. |

Spark's vector search needs **pgvector 0.8 or later** (it uses the iterative
index scan added in 0.8). `pgvector/pgvector:pg16` ships a recent 0.8 release; an
external PostgreSQL needs the `vector` extension at 0.8 or later.

Credentials come only from an env file you write:

```bash
cp deploy/spark.env.example deploy/spark.env
# edit deploy/spark.env: replace every value ending in -dev-only
docker compose -p spark -f deploy/compose.yaml up -d
docker compose -p spark -f deploy/compose.yaml run --rm web identity create-admin --email admin@example.com
```

`deploy/spark.env` is ignored by git. Compose variables:

| Variable | Default | Purpose |
|---|---|---|
| `SPARK_IMAGE` | `ghcr.io/cognisn/spark:0.3.0` | The image to run (for example a local build). |
| `SPARK_ENV_FILE` | `spark.env` (next to the Compose file) | The env file with credentials. |
| `SPARK_BIND` | `127.0.0.1` | The address the web port is published on. |
| `SPARK_WEB_PORT` | `8000` | The published web port. |

Standby workers: `docker compose -p spark -f deploy/compose.yaml up -d --scale worker=2`.

The web and worker containers share the `spark-data` volume, so they share the
configuration and the secrets file (`SPARK__SECRETS__FILE_PATH`), and the
`spark-files` volume, where users' file areas live (`SPARK__FILES__ROOT`). The web
role creates the instance's identity in the secrets file on first start, and the
worker waits for the web role to be healthy before starting.

## Upgrade

1. Pull or build the new image and set `SPARK_IMAGE` to it.
2. `docker compose -p spark -f deploy/compose.yaml up -d`

The `migrate` service runs `spark migrate` against the new schema before the new
web and worker containers start; both refuse to start on a database behind the
schema. To migrate by hand:

```bash
docker compose -p spark -f deploy/compose.yaml run --rm migrate
```

Back up the `postgres-data` volume before upgrading.

## Published images

Images are published to `ghcr.io/cognisn/spark` from a `v*` tag or a manual run
of the "Container publish" workflow, tagged with the version only: release
candidates (`0.3.0rc1`) and final versions (`0.3.0`). There is no `latest` tag.
