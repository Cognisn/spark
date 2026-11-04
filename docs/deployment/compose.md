# Compose Deployment

`deploy/compose.yaml` runs a complete Spark server on one Docker host: PostgreSQL
with pgvector, a one-shot migration, the web role and the worker role. This page
describes what each part does and how to start it. Commands are run from a checkout
of the repository (or a copy of its `deploy/` folder) and use the Compose project
name `spark`; keep that name, or your own, the same on every command.

## Services

| Service | Command | What it does |
|---|---|---|
| `postgres` | the image's own | `pgvector/pgvector:pg16`, with its data in the `postgres-data` volume. Not published on the host. |
| `migrate` | `spark migrate` | Brings the database to the current schema (Spark's chain, then warden's), then exits. Runs once each time the stack starts, after PostgreSQL is healthy. |
| `web` | `spark web` | The web interface on port 8000, published on `127.0.0.1:8000` by default. Starts only after `migrate` succeeds. |
| `worker` | `spark worker` | Scheduled actions, knowledge graph builds, warden's pruning and, with a retention, its audit purge. Starts after `migrate` succeeds and `web` is healthy. |

All four share one image (`SPARK_IMAGE`, default `ghcr.io/cognisn/spark:0.3.0`), and
the three Spark services share one definition: the env file, `SPARK__MODE=server`,
konfig's encrypted secrets file on the data volume, and `SPARK__FILES__ROOT`.

Web and worker refuse to start on a database behind the current schema, and the
worker also refuses personal mode and SQLite. A schema problem therefore shows as
`migrate` failing (`docker compose -p spark -f deploy/compose.yaml logs migrate`)
rather than as a half-working server.

Every Spark service connects as `POSTGRES_USER`, the owner of both schemas. Only
`migrate` needs to: web and worker only read the schema at start-up, so they can run
as login roles that own nothing and hold no `CREATE` on the database, with the audit
purge on a role of its own. [Database roles](database.md#restricted-roles) gives the
grants, the env files and the override.

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
| any `SPARK__<SECTION>__<KEY>` | Spark | Any other start-up setting, for example `SPARK__LOGGING__LEVEL=INFO` or `SPARK__WARDEN__SSO__ENABLED=true` |

`deploy/spark.env` is ignored by git. Keep it readable only by the account that runs
Compose (`chmod 600 deploy/spark.env`).

`database.url` may instead name a secret: `SPARK__DATABASE__URL=secret://database_url`,
with the URL kept in the secrets store under `database_url` (see
[placing a secret](secrets.md#placing-a-secret-in-the-store)). Compose still needs the
password in `POSTGRES_PASSWORD` for the bundled database.

Compose itself reads four variables, from the shell or from a `.env` file next to
`compose.yaml`:

| Variable | Default | Purpose |
|---|---|---|
| `SPARK_IMAGE` | `ghcr.io/cognisn/spark:0.3.0` | The image, pinned to a version (there is no `latest` tag) |
| `SPARK_ENV_FILE` | `spark.env` next to the Compose file | The env file above |
| `SPARK_BIND` | `127.0.0.1` | The host address the web port is published on |
| `SPARK_WEB_PORT` | `8000` | The published web port |

Leave `SPARK_BIND` at `127.0.0.1` with the reverse proxy on the same host. Spark
must never be reached except through the proxy: the sign-in cookies need https, and
the Origin check accepts only the public address.

## First start

```bash
docker compose -p spark -f deploy/compose.yaml up -d
docker compose -p spark -f deploy/compose.yaml ps -a
```

`migrate` should show `Exited (0)`, `web` `healthy` and `worker` `Up`. On first start:

- `migrate` creates the `spark` and `warden` schemas and the `vector` extension, and
  prints the head revision of each chain (`spark: ...`, `warden: ...`).
- `web` creates warden's signing key, its TOTP key and Spark's CSRF key in the
  secrets file. This is why the worker waits for `web` to be healthy.
- `worker` logs `is active (holds the worker lock)`.

```bash
docker compose -p spark -f deploy/compose.yaml logs worker
```

Then [create the first administrator](identity.md#the-first-administrator).

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
docker compose -p spark -f deploy/compose.yaml -f deploy/compose.override.yaml up -d
```

The proxy reaches the replicas as `web:8000`, which Docker resolves to each of them
in turn. The [reverse proxy page](reverse-proxy.md) gives the Caddyfile.

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
