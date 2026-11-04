# Backups and Upgrades

## Backups

A server's state is in four places. Back up the first three together, and keep the
fourth apart from them.

| What | Where | Holds |
|---|---|---|
| The database | `postgres-data` volume, or your external PostgreSQL | Every user's data, instance settings, warden's users, sessions and audit trail, the vectors |
| The data volume | `spark-data` (`/var/lib/spark`) | `config.yaml`, logs and konfig's encrypted secrets file |
| The files volume | `spark-files` (`/var/lib/spark-files`) | Users' uploads and documents |
| `KONFIG_MASTER_KEY` | the env file | The key to the secrets file; see [the master key](secrets.md#the-master-key) |

The `spark-models` volume needs no backup: the embedding model is downloaded again.
With AWS Secrets Manager, the secrets are in AWS instead of the data volume, and
Secrets Manager keeps their versions.

With the stack running:

```bash
docker compose -p spark -f deploy/compose.yaml exec -T postgres \
    sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > spark-db.dump
docker compose -p spark -f deploy/compose.yaml exec -T web tar -C /var/lib/spark -cf - . > spark-data.tar
docker compose -p spark -f deploy/compose.yaml exec -T web tar -C /var/lib/spark-files -cf - . > spark-files.tar
```

`pg_dump` takes a consistent snapshot while users carry on. The two `tar` copies are
taken a little after it, so a file uploaded in between is on disk without its
database row; that is harmless. Store the three files encrypted and off the host:
`spark-data.tar` holds the encrypted secrets, and the dump holds every conversation.

Test a restore now and then, on a separate host or Compose project.

## Restoring

Into a stack with the same image version as the backup:

```bash
docker compose -p spark -f deploy/compose.yaml stop web worker
docker compose -p spark -f deploy/compose.yaml exec -T postgres \
    sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < spark-db.dump
docker compose -p spark -f deploy/compose.yaml run --rm --no-deps -T --entrypoint tar web \
    -C /var/lib/spark -xf - < spark-data.tar
docker compose -p spark -f deploy/compose.yaml run --rm --no-deps -T --entrypoint tar web \
    -C /var/lib/spark-files -xf - < spark-files.tar
docker compose -p spark -f deploy/compose.yaml up -d
```

If you run with override files (restricted roles, replicas), pass them to the final
`up -d` as well. The env file must carry the same `KONFIG_MASTER_KEY` the backup was
taken with. On a
new host, start only the database first (`up -d postgres`), restore, then start the
rest. After a restore, users sign in again only if their sessions postdate the
backup.

With [restricted roles](database.md#restricted-roles), the dump carries the grants;
create the two login roles first on a new database server.

## Upgrades

1. Read the release notes ([CHANGELOG.md](../../CHANGELOG.md)) for the new version.
2. [Back up](#backups).
3. Set the new image, pinned to its version, in the `.env` file next to the Compose
   file or in the shell:

   ```
   SPARK_IMAGE=ghcr.io/cognisn/spark:0.3.0
   ```

4. Pull it and restart:

   ```bash
   docker compose -p spark -f deploy/compose.yaml pull
   docker compose -p spark -f deploy/compose.yaml up -d
   ```

`up` recreates the containers on the new image. `migrate` runs first and brings the
database to the new schema (Spark's chain, then warden's); the new web and worker
containers start only after it succeeds. Users see the server stop briefly while the
containers are replaced.

To migrate without starting anything else, for example to see what it does:

```bash
docker compose -p spark -f deploy/compose.yaml run --rm migrate
```

It prints the head of each chain (`spark: <revision>`, `warden: <revision>`) and is
safe to run again.

Web and worker refuse to start on a database behind their schema ("the database is
at revision ... but this SPARK needs ...; run `spark migrate`"), and on one ahead of
it ("the database is at revision ..., which this SPARK does not know ...; upgrade
SPARK"), so a mistake shows at once instead of as wrong behaviour.

> Not run for this guide: an upgrade from one published image to the next, since no
> earlier 0.3.0 image is published. `run --rm migrate` on a migrated database, and
> `up -d` re-running `migrate` before web and worker, were run.

With [restricted roles](database.md#restricted-roles), re-run warden's grants (step 3
of that section, and the worker's `DELETE` grant if a retention is set) after the
upgrade.

### Release candidates

Release candidates are tagged `0.3.0rc1`, `0.3.0rc2` and so on, and the final release
`0.3.0`. Move from a candidate to the next, or to the final release, the same way.
There is no `latest` tag to follow.

## Rolling back

`spark migrate` moves forward only. To go back to an earlier version, stop the stack,
[restore](#restoring) the backup taken before the upgrade, set `SPARK_IMAGE` back, and
start again. Do not start the earlier image on the upgraded database: it refuses, as
above.

## Other routine work

- **Logs**: `docker compose -p spark -f deploy/compose.yaml logs --since 1h web worker`.
- **Disk**: the database, `spark-files` (bounded by `files.quota_mb` per user) and the
  logs grow; the audit trail grows for ever unless a
  [retention](database.md#wardens-audit-trail-and-its-purge) is set.
- **Workers**: the worker logs `is active` or `standing by` when its state changes;
  scheduled actions and knowledge graph builds wait while no worker is running.
- **Users and sessions**: warden's expired tokens, challenges and sign-in throttles are
  pruned hourly by the active worker.
