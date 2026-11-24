# Compose Deployment

`deploy/compose.yaml` runs a complete Spark server on one Docker host: PostgreSQL
with pgvector, a one-shot migration and bootstrap, the web role, the worker role and a
local mail catcher. It comes up with nobody at a keyboard: the first administrator is
invited by email from the env file's settings. This page
describes what each part does and how to start it. Commands are run from a checkout
of the repository (or a copy of its `deploy/` folder) and use the Compose project
name `spark`; keep that name, or your own, the same on every command.

## Services

| Service | Command | What it does |
|---|---|---|
| `postgres` | the image's own | `pgvector/pgvector:pg16`, with its data in the `postgres-data` volume. Not published on the host. |
| `mailpit` | the image's own | A mail catcher (`axllent/mailpit`): every invitation and password reset lands in its inbox at `http://127.0.0.1:8025`. It also publishes the web port, because the Spark services share its network namespace (below). |
| `migrate` | `spark migrate && spark bootstrap` | Brings the database to the current schema (Spark's chain, then warden's), then runs [bootstrap](identity.md#bootstrap): the restricted database logins if configured, the instance identity, and the first administrator's invitation. Runs once each time the stack starts, after PostgreSQL is healthy; a later run sends nothing. |
| `web` | `spark web` | The web interface on port 8000, published on `127.0.0.1:8000` by default. Starts only after `migrate` succeeds. |
| `worker` | `spark worker` | Scheduled actions, knowledge graph builds, warden's pruning and, with a retention, its audit purge. Starts after `migrate` succeeds. |

The three Spark services share one image (`SPARK_IMAGE`, default
`ghcr.io/cognisn/spark:0.3.0`) and one definition: the env file, `SPARK__MODE=server`,
konfig's encrypted secrets file on the data volume, and `SPARK__FILES__ROOT`.

Spark sends plain-text SMTP only to a loopback host, so the Spark services share the
mail catcher's network namespace (`network_mode: service:mailpit`): the catcher's SMTP
port is `127.0.0.1:1025` inside every Spark container, and the web port is published
by `mailpit`. To send real mail instead, see [a real mail server](#a-real-mail-server).

Web and worker refuse to start on a database behind the current schema, and the
worker also refuses personal mode and SQLite. A schema problem therefore shows as
`migrate` failing (`docker compose -p spark -f deploy/compose.yaml logs migrate`)
rather than as a half-working server.

By default every Spark service connects as `POSTGRES_USER`, the owner of both
schemas. Only `migrate` needs to: with `SPARK__BOOTSTRAP__DATABASE__WEB_URL` and
`SPARK__BOOTSTRAP__DATABASE__WORKER_URL` set, bootstrap creates login roles for web
and worker that own nothing, with exactly their grants, and the audit purge gets a
role of its own. [Database roles](database.md#restricted-roles) gives the settings and
the override that gives web and worker their own URLs.

## Volumes

| Volume | Mounted at | Holds | Shared by |
|---|---|---|---|
| `postgres-data` | `/var/lib/postgresql/data` | The database | `postgres` |
| `spark-data` | `/var/lib/spark` | `config/spark/config.yaml`, logs, and konfig's encrypted secrets file `data/spark/secrets.enc` (with its lock file) | `migrate`, `web`, `worker` |
| `spark-files` | `/var/lib/spark-files` | Users' file areas: chat uploads, documents Spark creates, imported bundles' files and skills | `migrate`, `web`, `worker` |
| `spark-models` | `/var/cache/spark/huggingface` | The sentence-transformers embedding model, downloaded on first use | `migrate`, `web`, `worker` |

Every web and worker container must see the **same** `spark-data` and `spark-files`:
the secrets file holds warden's signing keys, and a file a user uploads through one
web replica is read by the worker or another replica. On one host the named volumes
do this. Across hosts, put both on shared storage that honours POSIX record locks
(NFS, including Amazon EFS, does), or use [AWS Secrets Manager](secrets.md#on-aws-secrets-manager)
for the secrets and shared storage for the files only.

The image runs as the unprivileged user `spark` (uid and gid 10001); a bind mount
used instead of a named volume must be writable by that uid.

### Hardening

`migrate`, `web` and `worker` run with a read-only root filesystem (`read_only: true`),
every Linux capability dropped (`cap_drop: [ALL]`), `no-new-privileges`, Docker's default
seccomp profile and an init process. Besides the volumes above, they write only to `/tmp`,
a tmpfs mounted `noexec,nosuid,nodev`: 2 GiB for `web` (uploads and bundle imports pass
through it), 256 MiB for `worker`, 64 MiB for `migrate`. Keep these settings in any
override; [Container hardening](container-hardening.md) explains each one and how to
check a running container.

## The env file

Credentials come only from an env file you write. Copy the example and replace every
value ending in `-dev-only`:

```bash
cp deploy/spark.env.example deploy/spark.env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

The second command prints a new key for `KONFIG_MASTER_KEY` (any Python with the
`cryptography` package will do). The file then holds:

| Variable | Read by | Purpose |
|---|---|---|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | `postgres` | The database owner the image creates on first start |
| `SPARK__DATABASE__URL` | Spark | `postgresql://USER:PASSWORD@postgres:5432/DB`, the same credentials; percent-encode special characters in the password |
| `SPARK__SERVER__PUBLIC_BASE_URL` | Spark | The https address users reach Spark on; see [Reverse proxy and TLS](reverse-proxy.md) |
| `KONFIG_MASTER_KEY` | Spark | The key to konfig's encrypted secrets file; see [Secrets](secrets.md) |
| `SPARK__BOOTSTRAP__BEDROCK__REGION`, `..._NAME`, `..._RESIDENCY_REGIONS`, `..._MODELS` | `migrate` | Optional, AWS only: add an Amazon Bedrock connection that uses the task role; see [AWS](aws.md#bedrock-through-the-task-role) |
| `SPARK__BOOTSTRAP__ADMIN__EMAIL`, `..._FIRST_NAME`, `..._LAST_NAME` | `migrate` | The first administrator, invited on the first start; see [Identity](identity.md#bootstrap) |
| `SPARK__MAIL__SMTP__HOST`, `..._PORT`, `..._TLS`, `..._SENDER_ADDRESS`, `..._SENDER_NAME` | `migrate` | Mail, stored as the instance's mail settings on the first start; the example sends to the mail catcher |
| any `SPARK__<SECTION>__<KEY>` | Spark | Any other start-up setting, for example `SPARK__LOGGING__LEVEL=INFO` or `SPARK__WARDEN__SSO__ENABLED=true` |

`deploy/spark.env` is ignored by git. Keep it readable only by the account that runs
Compose (`chmod 600 deploy/spark.env`).

`database.url` may instead name a secret: `SPARK__DATABASE__URL=secret://database_url`,
with the URL kept in the secrets store under `database_url` (see
[placing a secret](secrets.md#placing-a-secret-in-the-store)). Compose still needs the
password in `POSTGRES_PASSWORD` for the bundled database.

Compose itself reads five variables, from the shell or from a `.env` file next to
`compose.yaml`:

| Variable | Default | Purpose |
|---|---|---|
| `SPARK_IMAGE` | `ghcr.io/cognisn/spark:0.3.0` | The image, pinned to a version (there is no `latest` tag) |
| `SPARK_ENV_FILE` | `spark.env` next to the Compose file | The env file above |
| `SPARK_BIND` | `127.0.0.1` | The host address the web port and the mail catcher's inbox are published on |
| `SPARK_WEB_PORT` | `8000` | The published web port |
| `SPARK_MAIL_PORT` | `8025` | The published port of the mail catcher's inbox |

Leave `SPARK_BIND` at `127.0.0.1` with the reverse proxy on the same host. Spark
must never be reached except through the proxy: the sign-in cookies need https, and
the Origin check accepts only the public address.

## First start

```bash
docker compose -p spark -f deploy/compose.yaml up -d
docker compose -p spark -f deploy/compose.yaml ps -a
```

`migrate` should show `Exited (0)`, `mailpit` `Up`, `web` `healthy` and `worker` `Up`.
On first start:

- `migrate` creates the `spark` and `warden` schemas and the `vector` extension, and
  prints the head revision of each chain (`spark: ...`, `warden: ...`).
- Then bootstrap creates warden's signing key, its TOTP key and Spark's CSRF key in the
  secrets file, stores the mail settings, invites the first administrator and prints
  what it did, ending `bootstrap: complete`. Web and worker never race to create the
  identity, so the worker no longer waits for web.
- `worker` logs `is active (holds the worker lock)`.

```bash
docker compose -p spark -f deploy/compose.yaml logs migrate worker
```

Open the mail catcher at `http://127.0.0.1:8025`, follow the invitation to set the
first administrator's password and two-step verification, and sign in.

A setting that is missing or invalid stops `migrate` with every problem listed by
name (for example `bootstrap.admin.last_name (SPARK__BOOTSTRAP__ADMIN__LAST_NAME) is
not set`), and web and worker do not start. Fix the env file and run `up -d` again.

### A real mail server

Set the `SPARK__MAIL__SMTP__...` values to your mail server (port 587 with `starttls`
or 465 with `implicit`; plain text is refused to anything but loopback), place its
password as `mail_smtp_password` ([placing a secret](secrets.md#placing-a-secret-in-the-store)),
and drop the catcher with an override, `deploy/compose.real-mail.yaml`:

```yaml
services:
  mailpit:
    profiles: ["mail-catcher"]
  migrate:
    network_mode: !reset null
    depends_on: !override
      postgres:
        condition: service_healthy
  web:
    network_mode: !reset null
    ports:
      - "${SPARK_BIND:-127.0.0.1}:${SPARK_WEB_PORT:-8000}:8000"
  worker:
    network_mode: !reset null
```

The mail settings are stored on the first start only; after that change them on
**Administration > Mail**. (This override was not run for this guide.)

The first chat or memory search on a new `spark-models` volume downloads the
embedding model (`all-MiniLM-L6-v2`, about 90 MB, from Hugging Face), so the host needs outbound https
the first time. Each replica shares the downloaded copy afterwards.

## Replicas

**Workers.** Any number may run against one database; one is active and holds a
PostgreSQL advisory lock, the others stand by and take over within about fifteen
seconds when it stops or loses its connection:

```bash
docker compose -p spark -f deploy/compose.yaml up -d --scale worker=2
```

The second worker logs `standing by (another worker is active)`. Every `up` without
`--scale` returns to one worker, so put the count in your own override file (below)
if you want standby workers permanently.

**Web.** Several web replicas can serve one database without sticky sessions:
sessions live in warden's tables, settings changes and saved secrets reach every
replica over PostgreSQL notifications, and knowledge graph builds run in the worker.
Two replicas cannot both publish the same host port, so run them behind a reverse
proxy on the Compose network, with the published port removed. A
`deploy/compose.override.yaml` (your own file, beside the repository's) can do both:

This needs [a real mail server](#a-real-mail-server) and its override first: web
replicas cannot share one network namespace.

```yaml
services:
  web:
    ports: !reset []
    deploy:
      replicas: 2
  worker:
    deploy:
      replicas: 2
  proxy:
    image: caddy:2.8.4
    ports:
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
    depends_on:
      web:
        condition: service_healthy
    restart: unless-stopped
volumes:
  caddy-data:
```

```bash
docker compose -p spark -f deploy/compose.yaml -f deploy/compose.real-mail.yaml \
    -f deploy/compose.override.yaml up -d
```

The proxy reaches the replicas as `web:8000`, which Docker resolves to each of them
in turn. The [reverse proxy page](reverse-proxy.md) gives the Caddyfile.

**Shared conversations across replicas.** People viewing a shared conversation may be
on different replicas from the person whose turn is running, and nothing here needs
sticky sessions for it:

- one turn at a time is kept by a lease in the database (`conversation_turns`): the
  turn's replica refreshes it every 15 seconds and gives it back when the turn ends,
  so a second sender on any replica is told to wait; a lease left by a replica that
  stopped is taken over after 45 seconds;
- the live view travels over PostgreSQL notifications on the `spark_conversation`
  channel. Each replica opens one extra database connection to listen while anyone
  on it is viewing a conversation or running a turn, and closes it when none is.
  Allow for it in the database's connection limit (one per web replica);
- the live view is `GET /stream/conversation` (Server-Sent Events). A proxy in front
  must not buffer it, as for `/stream/chat`.

## pgvector

Spark's vector search needs **pgvector 0.8 or later**: it filters each search to the
signed-in user inside the index scan, using the iterative scan added in 0.8. The
`pgvector/pgvector:pg16` image ships a 0.8 release. To check:

```bash
docker compose -p spark -f deploy/compose.yaml exec -T postgres \
    sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT extversion FROM pg_extension WHERE extname = '\''vector'\''"'
```

An external PostgreSQL needs the extension available at 0.8 or later; see
[Database](database.md#an-external-postgresql).

## Files

Users' file areas are on the `spark-files` volume (`SPARK__FILES__ROOT`). Server mode
needs `files.root`: without it, uploads and the file tools answer that file storage is
not available. Administrators set each user's quota
(`files.quota_mb`, default 1024) and the largest upload (`files.max_upload_mb`,
default 50) on the instance settings page; a bundle import is checked against the
quota too. The reverse proxy must accept request bodies at least as large as the
largest upload, and up to 1 GB if administrators import user bundles.

## Logs

```bash
docker compose -p spark -f deploy/compose.yaml logs --since 1h web worker
```

Spark also writes a log folder per start under `/var/lib/spark/state/spark/logs` on
the data volume, keeping the last ten (`logging.retention_runs`). Set
`SPARK__LOGGING__FORMAT=json` for one JSON object per line.

## Stopping and removing

```bash
docker compose -p spark -f deploy/compose.yaml down
```

`down` keeps the volumes. `down -v` **deletes them**, with every user's data and the
secrets file: take a [backup](operations.md#backups) first.
