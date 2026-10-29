# Running Spark in containers

Spark ships one container image with two roles, plus a migration command:

| Command | Role |
|---|---|
| `spark web` (the image default) | The web interface. Plain `spark` is an alias. |
| `spark worker` | Background duties in server mode: scheduled actions, and later releases' periodic jobs. |
| `spark migrate` | Brings the database to the current schema, then exits. |

The image is meant for **server mode** (PostgreSQL). This page covers building
the image, the roles, the Compose file in `deploy/` and upgrades. A full server
deployment guide (identity, TLS, backups) follows in a later 0.3.0 build.

## Build

```bash
docker build -t spark:dev .
```

The build has two stages:

- **builder**: a virtual environment with the CPU-only PyTorch wheel (from the
  PyTorch CPU index; the default Linux wheel pulls in the NVIDIA CUDA runtime),
  then Spark and its dependencies. The licence audit (`scripts/licence_audit.py`)
  runs against exactly this environment, so a dependency that breaks the licence
  policy fails the build. pip, setuptools and wheel are then removed.
- **runtime**: `python:3.12-slim` with `libmagic1`, the virtual environment
  copied in, no compiler and no pip. Spark runs as the unprivileged user
  `spark` (uid and gid 10001).

`.dockerignore` limits the build context to `pyproject.toml`, `README.md`,
`LICENSE`, `src/` and the licence audit script: no tests, fixtures, local
databases, configuration or env files reach the image.

`deploy/check-image.sh IMAGE` checks a built image: it fails if the image runs as
root, carries a compiler or pip, has no healthcheck, contains tests, or if its
worker role does not refuse personal mode. CI runs it on every push and pull
request.

## Image layout

| Path | Purpose |
|---|---|
| `/var/lib/spark` (volume) | Configuration (`config/spark/config.yaml`), data, logs and konfig's encrypted secrets file. Share it between the web and worker containers. |
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
- **File**: `config.yaml` in the data volume at
  `/var/lib/spark/config/spark/config.yaml` (created from the template on first
  start). Environment values override the file.
- **Secrets**: provider keys and other secrets entered in Spark are kept in
  konfig's encrypted secrets file in the data volume
  (`SPARK__SECRETS__BACKEND=encrypted_file`), keyed by `KONFIG_MASTER_KEY`. When
  Spark is deployed into AWS, konfig can use AWS Secrets Manager instead
  (`KONFIG_AWS_SECRETS_MANAGER`).

## Roles

### Web

`spark web` serves the interface on port 8000. Until warden sign-in arrives in a
later 0.3.0 build, the web role signs in with the one-time code it logs at start
(`docker compose logs web`), and sessions are held in the process. So run one web
replica for now. Several replicas behind a reverse proxy become useful once
sessions move to warden.

### Worker

`spark worker` runs scheduled actions. It refuses to start in personal mode, on
SQLite, or on a database behind the current schema.

Any number of workers may run against one database. One is **active**: it holds a
PostgreSQL advisory lock on a connection of its own. The others **stand by** and
retry every five seconds (`worker.standby_seconds`). When the active worker stops,
or its connection to the database is lost, PostgreSQL releases the lock and a
standby worker takes over. A worker that takes the lock waits about ten seconds
(twice `worker.check_seconds`) before it starts its duties, so a worker that has
just lost its connection has time to notice and stop first. Each scheduled action
also takes a lock in the database before it runs.

The worker logs `standing by` or `is active` when its state changes.

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

Credentials come only from an env file you write:

```bash
cp deploy/spark.env.example deploy/spark.env
# edit deploy/spark.env: replace every value ending in -dev-only
docker compose -p spark -f deploy/compose.yaml up -d
docker compose -p spark -f deploy/compose.yaml logs web    # the sign-in code
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
configuration and the secrets file. The web role creates the instance's identity
in that file on first start, and the worker waits for the web role to be healthy
before starting.

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
