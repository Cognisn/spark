# Portainer

`deploy/portainer/stack.yaml` runs a Spark server as a Portainer stack: PostgreSQL
with pgvector, a one-shot migration and first-run bootstrap, the web role, the worker
role and a local mail catcher. It is the
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
  conditions (`migrate`, which also runs `spark bootstrap`, must finish before web and worker start) that Swarm stacks
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

Like `deploy/compose.yaml`, the stack runs `migrate`, `web` and `worker` with a read-only
root filesystem, every capability dropped, `no-new-privileges`, Docker's default seccomp
profile and a tmpfs `/tmp` ([Container hardening](container-hardening.md)). Portainer
applies them as written; do not add capabilities or turn on privileged mode for these
services in Portainer's container editor.

## The variables

| Variable | Required | Purpose |
|---|---|---|
| `SPARK_IMAGE` | yes | The image and version, for example `ghcr.io/cognisn/spark:0.3.0rc1` |
| `SPARK_PUBLIC_BASE_URL` | yes | The https address users reach Spark on, through the proxy, for example `https://spark.example.com`; see [server.public_base_url](reverse-proxy.md#serverpublic_base_url) |
| `POSTGRES_PASSWORD` | yes | The database owner's password. Spark's database URL is built from it, so use letters, digits, `-` and `_` only |
| `KONFIG_MASTER_KEY` | yes | The key to konfig's encrypted secrets file; see [the master key](secrets.md#the-master-key) |
| `SPARK__BOOTSTRAP__ADMIN__EMAIL` | yes | The first administrator's address: `spark bootstrap` invites them by mail on the first start ([Identity](identity.md#bootstrap)) |
| `SPARK__BOOTSTRAP__ADMIN__FIRST_NAME`, `SPARK__BOOTSTRAP__ADMIN__LAST_NAME` | yes | Their name, as the invitation shows it |
| `SPARK__BOOTSTRAP__BEDROCK__REGION`, `SPARK__BOOTSTRAP__BEDROCK__NAME`, `SPARK__BOOTSTRAP__BEDROCK__RESIDENCY_REGIONS`, `SPARK__BOOTSTRAP__BEDROCK__MODELS` | no | AWS only, commented out in the stack: add an Amazon Bedrock connection that uses the task role ([AWS](aws.md#bedrock-through-the-task-role)) |
| `POSTGRES_USER`, `POSTGRES_DB` | no | The database owner and database name (default `spark` for both) |
| `SPARK_BIND` | no | The host address the web port is published on (default `127.0.0.1`) |
| `SPARK_WEB_PORT` | no | The published web port (default `8000`) |
| `SPARK_MAIL_PORT` | no | The port of the mail catcher's inbox, published on `127.0.0.1` only (default `8025`) |
| `SPARK__MAIL__SMTP__HOST`, `PORT`, `TLS`, `SENDER_ADDRESS`, `SENDER_NAME` | no | Where the invitation is sent, stored as the instance's mail settings on the first start. They default to the mail catcher (`127.0.0.1`, `1025`, `none`, `spark@example.com`, `Spark`); see [the mail catcher](#the-mail-catcher) |
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
   SPARK__BOOTSTRAP__ADMIN__EMAIL=CHANGEME@example.com
   SPARK__BOOTSTRAP__ADMIN__FIRST_NAME=CHANGEME
   SPARK__BOOTSTRAP__ADMIN__LAST_NAME=CHANGEME
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

**Containers** lists five, prefixed with the stack name:

| Container | Expected state |
|---|---|
| `spark-postgres-1` | running, healthy |
| `spark-mailpit-1` | running: the mail catcher, which also publishes the web port |
| `spark-migrate-1` | exited with code 0: it runs `spark migrate && spark bootstrap` at each deployment, then stops |
| `spark-web-1` | running, healthy (it has no network of its own: it shares the mail catcher's) |
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
  Join network**) and send requests to `http://spark-mailpit-1:8000` (the web container shares the mail catcher's network). The published
  port on `127.0.0.1` can stay; nothing outside the host reaches it. With Nginx
  Proxy Manager, add a proxy host for `spark.example.com` with scheme `http`,
  forward host `spark-mailpit-1` and port `8000`, request a certificate on its SSL
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

A new server has no users, and nobody needs to create one by hand: `spark-migrate-1`
runs `spark bootstrap` after `spark migrate`, which invites the administrator named
by `SPARK__BOOTSTRAP__ADMIN__EMAIL` and its two name variables. The invitation lands
in [the mail catcher](#the-mail-catcher) (or your own mail server). Follow its link,
set a password, and sign in at your public address, then set up two-step
verification. [Identity](identity.md#bootstrap) lists the bootstrap settings and what
the step does, and is safe to run again.

Then add a model connection on **Administration > Connections and models**, choose its models and
assign it, and set up mail on **Administration > Mail**: [the checklist](index.md#checklist)
gives the order, and [Identity](identity.md) covers roles, invitations and single
sign-on.

If `spark-migrate-1` exits non-zero, its log names the problem (an incomplete
setting, or an invitation that could not be sent): fix it and redeploy. To send the
invitation again, or to restore an administrator when no one can sign in, see
[Identity](identity.md#restoring-an-administrator); run those commands as
`docker exec -it spark-web-1 spark ...`.

## The mail catcher

`spark-mailpit-1` catches the invitation and password resets, so the stack comes up
with no mail server: its inbox is at `http://127.0.0.1:8025` on the Docker host
(published on `127.0.0.1` only, whatever `SPARK_BIND` says; reach it from your own
computer with an SSH tunnel). Spark's containers share its network, because Spark
sends plain-text mail only to a loopback host, so it also publishes the web port.

For real mail, set the `SPARK__MAIL__SMTP__*` variables to your mail server (port
587 with `starttls`, or 465 with `implicit`) before the first deployment, or set mail
on **Administration > Mail** afterwards. The password goes in the secrets store as
`mail_smtp_password` ([Secrets](secrets.md)). [A real mail
server](compose.md#a-real-mail-server) explains when to drop the catcher.

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
no sticky sessions are needed. Replicas also need [a real mail server](compose.md#a-real-mail-server): drop the catcher from the stack first, and give `web` its own network again.

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

> Checked for this guide: the stack file's structure is tested (`tests/test_deploy_stacks.py`)
> and its Compose model rendered with `docker compose config`. The earlier stack, before
> bootstrap, was deployed as Portainer deploys a web editor stack on Docker Compose 5.5.1
> against an image built from this repository (the refusal while a required variable is
> missing, first start, the worker holding its lock, the backup commands). Not run: the
> bootstrap stack end to end, Portainer itself, Nginx Proxy Manager and a certificate for
> a real host name.
