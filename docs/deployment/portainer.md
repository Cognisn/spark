# Portainer

`deploy/portainer/stack.yaml` runs a Spark server as a Portainer stack: PostgreSQL
with pgvector, a one-shot migration, the web role and the worker role. It is the
same server as [Compose deployment](compose.md) describes, written for Portainer:
every setting comes from the stack's environment variables, with no env file or
other file beside it, so it works from Portainer's web editor as well as from the
Git repository.

The rest of this guide applies unchanged: [Secrets](secrets.md),
[Reverse proxy and TLS](reverse-proxy.md), [Identity](identity.md),
[Database](database.md) and [Backups and upgrades](operations.md). Commands there
use `docker compose -p spark -f deploy/compose.yaml`; on a Portainer host, use the
`docker exec` forms given below.

## What you need

- Portainer (Community or Business Edition) managing a **standalone Docker**
  environment: a Docker host, through the Portainer agent or the local socket. Not
  a Swarm or Kubernetes environment: the stack relies on Compose start-up
  conditions (`migrate` must finish before web and worker start) that Swarm stacks
  ignore.
- At least 4 GB of memory and 10 GB of disk on the Docker host, and outbound https
  for the image and, once, the embedding model.
- A DNS name for the server (this guide uses `spark.example.com`) and a TLS reverse
  proxy for it: one on the host, or a proxy container on the same Docker host such
  as Nginx Proxy Manager, Traefik or Caddy. Spark serves plain http on port 8000
  and must only be reached through the proxy: the sign-in cookies need https, and
  the Origin check accepts only the public address.

## The image

Release images are on `ghcr.io/cognisn/spark`, tagged with the version only
(`0.3.0rc1`, `0.3.0`; there is no `latest`). Portainer pulls the image when it deploys
the stack.

To use an image you built yourself, build it on the Docker host (or push it to your
own registry) and give its tag as `SPARK_IMAGE`:

```bash
docker build -t spark:0.3.0rc1 .
```

## The variables

| Variable | Required | Purpose |
|---|---|---|
| `SPARK_IMAGE` | yes | The image and version, for example `ghcr.io/cognisn/spark:0.3.0rc1` |
| `SPARK_PUBLIC_BASE_URL` | yes | The https address users reach Spark on, through the proxy, for example `https://spark.example.com`; see [server.public_base_url](reverse-proxy.md#serverpublic_base_url) |
| `POSTGRES_PASSWORD` | yes | The database owner's password. Spark's database URL is built from it, so use letters, digits, `-` and `_` only |
| `KONFIG_MASTER_KEY` | yes | The key to konfig's encrypted secrets file; see [the master key](secrets.md#the-master-key) |
| `POSTGRES_USER`, `POSTGRES_DB` | no | The database owner and database name (default `spark` for both) |
| `SPARK_BIND` | no | The host address the web port is published on (default `127.0.0.1`) |
| `SPARK_WEB_PORT` | no | The published web port (default `8000`) |
| `SPARK_LOG_LEVEL` | no | `INFO` by default |

Generate the master key on any machine with Docker:

```bash
docker run --rm --entrypoint python ghcr.io/cognisn/spark:0.3.0rc1 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep a copy of `KONFIG_MASTER_KEY` outside Portainer: without it the secrets file
cannot be read. Portainer stores the stack's variables in its own database and shows
them to anyone who can edit the stack, and the master key and database password
open everything, so limit who can manage this stack. On AWS, [Secrets
Manager](secrets.md#on-aws-secrets-manager) keeps the secrets out of Portainer
altogether.

Other start-up settings (single sign-on, trusted proxies, logging format) are
`SPARK__<SECTION>__<KEY>` entries: add them to the `environment` block under
`x-spark` at the top of the stack file.

## Deploy from the web editor

1. **Stacks > Add stack**. Name it `spark` (the name prefixes every container,
   volume and network: `spark-web-1`, `spark_spark-data`, `spark_default`).
2. Choose **Web editor** and paste the contents of `deploy/portainer/stack.yaml`.
3. Under **Environment variables**, switch to **Advanced mode** and enter the
   variables, one `NAME=value` per line:

   ```
   SPARK_IMAGE=ghcr.io/cognisn/spark:0.3.0rc1
   SPARK_PUBLIC_BASE_URL=https://spark.example.com
   POSTGRES_PASSWORD=replace-with-a-long-random-password
   KONFIG_MASTER_KEY=replace-with-the-generated-key
   ```

4. **Deploy the stack**. If a required variable is missing, Portainer shows the
   deployment error naming it.

## Deploy from the Git repository

**Stacks > Add stack > Repository**, with the repository URL
`https://github.com/Cognisn/spark`, the reference of the release you deploy (for
example `refs/tags/v0.3.0rc1`, once that tag exists) and the Compose path
`deploy/portainer/stack.yaml`. Enter the same environment variables, and deploy.
Pin the reference to a release, never a branch, so the stack changes only when you
change it.

## Check it

**Containers** lists four, prefixed with the stack name:

| Container | Expected state |
|---|---|
| `spark-postgres-1` | running, healthy |
| `spark-migrate-1` | exited with code 0: it brings the database to the current schema at each deployment, then stops |
| `spark-web-1` | running, healthy |
| `spark-worker-1` | running; its log says `is active (holds the worker lock)` |

## The reverse proxy

Point the proxy at the web container and follow
[what the proxy must do](reverse-proxy.md#what-the-proxy-must-do): pass the `Host`
header through, turn response buffering off, allow long reads and large uploads.
Then name the proxy for the audit trail with `SPARK__WARDEN__AUDIT__TRUSTED_PROXIES`
(see [client addresses](reverse-proxy.md#client-addresses-in-the-audit-trail)).

- **A proxy on the host** (Caddy or nginx installed on the machine): keep
  `SPARK_BIND` at `127.0.0.1` and proxy to `127.0.0.1:8000`, as in the
  [Caddy](reverse-proxy.md#caddy) and [nginx](reverse-proxy.md#nginx) examples.
- **A proxy container on the same Docker host**: connect it to the stack's network
  (**Networks > spark_default**, or the proxy container's **Connected networks >
  Join network**) and send requests to `http://spark-web-1:8000`. The published
  port on `127.0.0.1` can stay; nothing outside the host reaches it. With Nginx
  Proxy Manager, add a proxy host for `spark.example.com` with scheme `http`,
  forward host `spark-web-1` and port `8000`, request a certificate on its SSL
  tab, and put this in its Advanced tab:

  ```nginx
  client_max_body_size 1g;
  proxy_buffering off;
  proxy_read_timeout 600s;
  ```

- **A proxy on another machine**: set `SPARK_BIND` to the Docker host's own address
  and allow only the proxy to reach that port (a host firewall rule). Never publish
  port 8000 to everyone.

## The first administrator

A new server has no users. Open the web container's console (**Containers >
spark-web-1 > Console**, command `/bin/sh`) and run:

```bash
spark identity create-admin --email admin@example.com
```

or, from a shell on the Docker host:

```bash
docker exec -it spark-web-1 spark identity create-admin --email admin@example.com
```

It asks for the password twice (at least 12 characters) and prints `administrator
<id>, created, administrator role assigned`. Sign in at your public address, set up
two-step verification, then enable a provider, choose the models offered and set up
mail on the instance settings page: [the checklist](index.md#checklist) gives the
order, and [Identity](identity.md) covers roles, invitations and single sign-on.

## Standby workers and web replicas

For a standby worker, edit the stack and add to the `worker` service:

```yaml
    deploy:
      replicas: 2
```

then **Update the stack**. The second worker logs `standing by (another worker is
active)` and takes over within about fifteen seconds if the active one stops.

Several web replicas need the proxy to reach them on the stack's network, because
two cannot publish the same port: remove the `ports` entry from `web`, give it
`deploy: replicas: 2`, and send the proxy to `http://web:8000` (Docker resolves the
service name to each replica in turn). [Replicas](compose.md#replicas) explains why
no sticky sessions are needed.

## Backups

From a shell on the Docker host, with the stack running:

```bash
docker exec spark-postgres-1 sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' > spark-db.dump
docker exec spark-web-1 tar -C /var/lib/spark -cf - . > spark-data.tar
docker exec spark-web-1 tar -C /var/lib/spark-files -cf - . > spark-files.tar
```

[Backups and upgrades](operations.md#backups) explains what each holds and how to
restore; the restore commands there work with `docker exec -i spark-postgres-1`
and a stopped web and worker. Keep the copies encrypted and off the host.

## Upgrading

1. Read the release notes ([CHANGELOG.md](../../CHANGELOG.md)) and take a backup.
2. **Stacks > spark > Editor**: change `SPARK_IMAGE` to the new version (and, for a
   Git stack, the reference), then **Update the stack** with **Re-pull image**
   selected.
3. `spark-migrate-1` runs again and upgrades the database before web and worker
   start; check that it exited with code 0.

Going back to an older version needs the backup taken before the upgrade: see
[rolling back](operations.md).

## Removing

Deleting the stack removes its containers and network but keeps its volumes
(**Volumes**, prefixed `spark_`). Deleting the volumes deletes the database, the
secrets file and every uploaded file: take a backup first.

> Checked for this guide: the stack file deployed as Portainer deploys a web editor
> stack, with `docker compose --env-file` and the variables above, on Docker Compose
> 5.5.1 against an image built from this repository: the refusal while a required
> variable is missing, first start, the first administrator from `docker exec`, the
> worker holding its lock, the web container reached as `spark-web-1:8000` and
> `web:8000` from a container joined to the stack's network, and the backup
> commands. The project name and port were changed to fit the test machine. Not
> run: Portainer itself, Nginx Proxy Manager and a certificate for a real host name.
