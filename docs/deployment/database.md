# Database

A Spark server keeps everything in one PostgreSQL database, in two schemas:

| Schema | Holds | Migrated by |
|---|---|---|
| `spark` | Conversations, memories, files' records, knowledge graphs, scheduled actions, usage, instance settings, vectors | Spark's Alembic chain (`spark.alembic_version`) |
| `warden` | Users, sessions, roles, invitations, API keys, single sign-on records, signing keys' public halves, the audit trail | warden's chain (`warden.alembic_version`) |

`spark migrate` runs Spark's chain and then warden's. The `vector` extension is created
in `public`. Every connection sets `TimeZone=UTC`.

## An external PostgreSQL

The Compose file's `postgres` service suits a single host. To use a managed or
existing server instead, point `SPARK__DATABASE__URL` at it:

```
SPARK__DATABASE__URL=postgresql://spark:PASSWORD@db.example.com:5432/spark?sslmode=verify-full
```

and switch the bundled database off with an override, `deploy/compose.external-db.yaml`:

```yaml
services:
  postgres:
    profiles: ["bundled-database"]
  migrate:
    depends_on: !reset {}
```

```bash
docker compose -p spark -f deploy/compose.yaml -f deploy/compose.external-db.yaml up -d
```

The `postgres` service then starts only if that profile is asked for. (This override
was not run for this guide, which used the bundled database throughout.)

It needs:

- **PostgreSQL 16** (the version Spark is tested on).
- **pgvector 0.8 or later**, available to the database. The first `spark migrate` runs
  `CREATE EXTENSION IF NOT EXISTS vector`, which needs a superuser or, on managed
  services, the provider's extension-creating role. If the role `migrate` connects as
  cannot create extensions, have an administrator create it first:
  `CREATE EXTENSION vector WITH SCHEMA public;`.
- **TLS** from Spark to the database: `sslmode=verify-full` in the URL, with the
  server's certificate authority in the system trust store or named by
  `sslrootcert`.
- **Connections**: each web replica and each worker opens a pool of up to 10
  connections, shared with warden, plus a few of its own (the worker's lock, the
  settings listener). Allow 25 per web replica and per worker as a start, and check
  `pg_stat_activity` under load.
- **LISTEN and NOTIFY** working end to end. A connection pooler in transaction mode
  (PgBouncer's default) breaks them, and with them settings and secrets reaching every
  replica, so connect directly or use session mode.

Managed PostgreSQL with pgvector 0.8 (for example Amazon RDS or Aurora PostgreSQL) was
not tried for this guide.

## Database roles

### The default: one owner

In the Compose file every service connects as `POSTGRES_USER`, which owns both
schemas. That is simple and works, but a table's owner bypasses grants, so the
database itself does not stop Spark's role from editing or deleting audit rows. The
audit trail is still hash-chained and signed, so verifying the chain shows tampering,
but nothing prevents it.

### Restricted roles

For an append-only audit trail at the database level, keep the owner for `migrate`
only and give web and worker their own login roles, which own nothing and need no
`CREATE` on the database: start-up only reads the schema, and only `spark migrate`
creates it. As the owner, with the stack already migrated:

**1. Create the roles.** Save as `roles.sql`, with your own passwords:

```sql
-- Login roles for the web and worker containers; migrate keeps the owner.
CREATE ROLE spark_web LOGIN PASSWORD 'replace-with-a-web-password';
CREATE ROLE spark_worker LOGIN PASSWORD 'replace-with-a-worker-password';
GRANT CONNECT ON DATABASE spark TO spark_web, spark_worker;
```

If you will set an audit retention, add the purge's own role to the same file (see
[the purge](#wardens-audit-trail-and-its-purge)):

```sql
-- Only the audit purge connects as this role.
CREATE ROLE spark_purger LOGIN PASSWORD 'replace-with-a-purger-password';
GRANT CONNECT ON DATABASE spark TO spark_purger;
```

**2. Grant Spark's schema.** Save as `grants.sql`:

```sql
-- Spark's own schema: read and write every table, use every sequence.
GRANT USAGE ON SCHEMA spark TO spark_web, spark_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA spark TO spark_web, spark_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA spark TO spark_web, spark_worker;
-- Spark's migration version table: read only.
REVOKE INSERT, UPDATE, DELETE ON spark.alembic_version FROM spark_web, spark_worker;
-- Tables and sequences that later migrations create (as the owner, spark).
ALTER DEFAULT PRIVILEGES FOR ROLE spark IN SCHEMA spark
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO spark_web, spark_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE spark IN SCHEMA spark
    GRANT USAGE, SELECT ON SEQUENCES TO spark_web, spark_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE spark IN SCHEMA warden
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO spark_web, spark_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE spark IN SCHEMA warden
    GRANT USAGE ON SEQUENCES TO spark_web, spark_worker;
```

Replace `spark` in `ON DATABASE spark` and `FOR ROLE spark` if your database or owner
is named differently. The purger role gets nothing in Spark's schema.

**3. Let warden write its own grants**, which allow every warden table except that
`audit_events` is insert and read only and the version table read only:

```bash
docker compose -p spark -f deploy/compose.yaml run --rm --no-deps -T --entrypoint warden web \
    audit grants --app-role spark_web > warden-web.sql
docker compose -p spark -f deploy/compose.yaml run --rm --no-deps -T --entrypoint warden web \
    audit grants --app-role spark_worker > warden-worker.sql
```

With the purger role, write the worker's file with it instead, so the one file also
gives `spark_purger` what the purge needs (read and append to `audit_events`, delete
from it, and read the signing keys) and nothing else:

```bash
docker compose -p spark -f deploy/compose.yaml run --rm --no-deps -T --entrypoint warden web \
    audit grants --app-role spark_worker --purger-role spark_purger > warden-worker.sql
```

`warden audit grants` only prints SQL; read the files before running them.

**4. Run the four files as the owner:**

```bash
for f in roles.sql grants.sql warden-web.sql warden-worker.sql; do
    docker compose -p spark -f deploy/compose.yaml exec -T postgres \
        sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q' < "$f"
done
```

**5. Give web and worker their own URLs.** Two env files beside `spark.env`, ignored
by git like it:

`deploy/web.env`:

```
SPARK__DATABASE__URL=postgresql://spark_web:replace-with-a-web-password@postgres:5432/spark
```

`deploy/worker.env`:

```
SPARK__DATABASE__URL=postgresql://spark_worker:replace-with-a-worker-password@postgres:5432/spark
```

With the purger role, `deploy/worker.env` also names the purge's connection:

```
SPARK__WORKER__AUDIT_PURGE_DATABASE_URL=postgresql://spark_purger:replace-with-a-purger-password@postgres:5432/spark
```

and an override, `deploy/compose.roles.yaml`:

```yaml
services:
  web:
    env_file:
      - path: web.env
        required: true
  worker:
    env_file:
      - path: worker.env
        required: true
```

```bash
docker compose -p spark -f deploy/compose.yaml -f deploy/compose.roles.yaml up -d
```

From now on, pass both files on every command. `migrate` still connects as the owner
from `spark.env`.

**6. Re-run step 3's SQL after every upgrade**, as warden asks, so its grants cover any
table a new version adds.

To check, as the owner:

```sql
SELECT has_table_privilege('spark_web', 'warden.audit_events', 'DELETE'),
       has_table_privilege('spark_web', 'warden.audit_events', 'UPDATE'),
       has_table_privilege('spark_worker', 'warden.audit_events', 'DELETE'),
       has_table_privilege('spark_worker', 'warden.audit_events', 'UPDATE'),
       has_database_privilege('spark_web', 'spark', 'CREATE'),
       has_database_privilege('spark_worker', 'spark', 'CREATE');
```

answers `f | f | f | f | f | f`. With the purger role,

```sql
SELECT has_table_privilege('spark_purger', 'warden.audit_events', 'DELETE'),
       has_table_privilege('spark_purger', 'warden.audit_events', 'UPDATE');
```

answers `t | f`.

Earlier 0.3.0 builds needed `GRANT CREATE ON DATABASE spark` for web and worker,
because start-up created the schema if it was missing. That is no longer needed; if
you granted it, take it back with
`REVOKE CREATE ON DATABASE spark FROM spark_web, spark_worker;`.

## warden's audit trail and its purge

warden keeps the audit trail for ever unless a retention is set. With
`SPARK__WARDEN__AUDIT__RETENTION_DAYS` (365 or more), the active worker purges rows
older than that once a day (`worker.audit_purge_seconds`): it verifies the oldest part
of the chain, appends a checkpoint signed with warden's key, and deletes the rows
before it, in one transaction. A broken chain is refused and the refusal audited, so a
purge cannot hide tampering.

The purge needs `DELETE` on `warden.audit_events`, the one place anything deletes
audit rows. Choose one:

| Arrangement | Who holds `DELETE` on `audit_events` |
|---|---|
| No retention (the default) | Nobody needs it. With restricted roles, no Spark role holds it |
| Retention, default Compose roles | The owner, which every service connects as |
| Retention, restricted roles | The purger role only, through `worker.audit_purge_database_url` |

`worker.audit_purge_database_url` (`SPARK__WORKER__AUDIT_PURGE_DATABASE_URL`) is read
by `spark worker` at start-up. When it is set, each purge opens a short-lived
connection of its own with it and runs entirely there, so the worker's own role keeps
only `INSERT` and `SELECT` on `audit_events`; nothing else uses it. When it is unset,
the purge runs on the worker's own connection (`database.url`), which with restricted
roles is refused and logged once a day.

Like `database.url`, it may be a `secret://<name>` reference to the secrets store
instead of a URL, so the purger's password stays out of the env file. A value that is
not a PostgreSQL URL, or a secret that is not set, stops the worker at start-up with
a message that names the setting but never the URL. Spark never logs the URL or its
password: a failed purge is logged by its kind only (for example
`Worker job warden-audit-purge failed (PoolTimeout)`), and the connection pool's own
warnings name the database host and user, never the password.

> Checked for this guide on the Compose stack, with an image built from this
> repository: steps 1 to 5 with the purger role (the second form of step 3 for the
> worker) and no `CREATE` on the database for any role, then web healthy, the
> worker active and its first purge connecting as `spark_purger` (seen in
> PostgreSQL's connection log), and both privilege checks answering as above. With a
> wrong purger password the worker stayed active, logged the purge failure by its kind
> only (the pool's warnings named the host and user, and no log held the password),
> and PostgreSQL logged the refused sign-in for `spark_purger`. Not run on the stack:
> step 3's first form for the worker (run for the previous build of this guide), a
> purge that deletes rows, since it needs a year of audit rows (the test suite runs
> one as a purger role with the minimum retention lowered), and the `secret://` form
> (covered by the test suite).
