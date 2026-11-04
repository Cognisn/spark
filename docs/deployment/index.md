# Deploying a Spark Server

This guide takes a Spark server from nothing to a running, multi-user service:
PostgreSQL with pgvector, the web and worker roles in containers, konfig secrets,
a TLS reverse proxy, warden identity (users, roles, single sign-on and mail),
backups and upgrades, and moving people over from personal mode.

Server mode is new in 0.3.0. A personal installation (one user, SQLite, the OS
keychain) needs none of this: see [Getting Started](../getting-started.md).

## The pages

| Page | Covers |
|---|---|
| [Compose deployment](compose.md) | The services, roles, volumes and replicas in `deploy/compose.yaml`, the env file, the migrate step, the files volume and pgvector |
| [Secrets](secrets.md) | konfig's encrypted store and `KONFIG_MASTER_KEY` off AWS, AWS Secrets Manager and its IAM policy on AWS, and how a saved secret reaches every replica |
| [Reverse proxy and TLS](reverse-proxy.md) | `server.public_base_url`, what the Origin check and the sign-in cookies need from the proxy, with Caddy and nginx examples |
| [Identity](identity.md) | The first administrator (`spark identity create-admin`), roles, two-step verification, single sign-on through warden's settings, and identity mail (`mail.smtp`) |
| [Database](database.md) | An external PostgreSQL, database roles, and the grants warden's audit purge needs on `warden.audit_events` |
| [Backups and upgrades](operations.md) | What to back up, restoring, upgrading with `spark migrate`, and rolling back |
| [Moving from personal mode](from-personal.md) | Exporting a personal user's bundle and importing it on the server as an administrator |

## What a server is made of

```
             users (browsers, API keys)
                       |
                 https://spark.example.com
                       |
           TLS reverse proxy (your own)
                       |
          web replicas: spark web (port 8000)
              |                     |
         PostgreSQL 16  <---  spark worker (one active, others standing by)
         with pgvector
              |
   spark-data volume (configuration, konfig secrets file)
   spark-files volume (users' file areas)
   spark-models volume (the embedding model)
```

- **One image, two roles** (`ghcr.io/cognisn/spark:<version>`): `spark web` serves
  the interface and may run as several replicas; `spark worker` runs scheduled
  actions, knowledge graph builds and warden's pruning, with one worker active at a
  time. `spark migrate` brings the database to the current schema.
- **PostgreSQL** holds every user's data, the instance settings, warden's users,
  sessions and audit trail, and the vector indexes (pgvector 0.8 or later).
- **konfig** holds every secret (provider keys, warden's signing keys, the SMTP
  password): in its encrypted store keyed by `KONFIG_MASTER_KEY`, or in AWS Secrets
  Manager. The database holds no secret.
- **warden** (`cognisn-warden`, installed with Spark) provides sign-in, two-step
  verification, invitations, password resets, single sign-on, roles, API keys and
  the audit trail. It runs inside the web and worker processes; there is no separate
  identity service to deploy.

## Checklist

1. A host with Docker and Compose, a DNS name for the server (this guide uses
   `spark.example.com`) and a TLS certificate for it.
2. [Write the env file and start the stack](compose.md#first-start).
3. [Keep `KONFIG_MASTER_KEY` safe](secrets.md#the-master-key), or
   [use AWS Secrets Manager](secrets.md#on-aws-secrets-manager).
4. [Put the reverse proxy in front](reverse-proxy.md) and set
   `server.public_base_url` to its address.
5. [Create the first administrator](identity.md#the-first-administrator), sign in
   and set up two-step verification.
6. On the instance settings page: enable a provider, choose the models offered,
   [set up mail](identity.md#mail), and optionally
   [single sign-on](identity.md#single-sign-on).
7. Invite users and give them roles.
8. [Schedule backups](operations.md#backups).

## Release images

Images are published to `ghcr.io/cognisn/spark` with the version only: release
candidates (`0.3.0rc1`, `0.3.0rc2`) and final releases (`0.3.0`). There is no
`latest` tag, so pin the version you deploy and change it deliberately when you
upgrade. The [container guide](../container.md) covers building the image yourself.

## How this guide was checked

The commands were run as written against Spark 0.3.0a18, on a Compose stack built
from this repository's `Dockerfile` with the `deploy/` files, under a different Compose
project name, on loopback ports, with `server.public_base_url` on a loopback https
address served by Caddy's internal certificate:

- first start, `ps`, the logs, the pgvector check (0.8.7), `identity create-admin`
  (twice), standby workers, `run --rm migrate`;
- the replicas override with Caddy (port and site name changed to loopback) in front
  of two web replicas and two workers, a GET of the sign-in page through it, a
  password sign-in through to the second factor, and the Origin and Host refusals;
- every step of the restricted database roles, the worker's `DELETE` grant and the
  privilege check; the external database override with `docker compose config` only;
- placing a secret, restarting web and worker, and a refused wrong master key;
- the backup and restore commands, with the restricted grants surviving the restore;
- `spark export --output` in personal mode in the image.

Not run, each said so on its page: AWS Secrets Manager (no AWS account), a real
single sign-on provider, delivery to a real mail server, the administrator's bundle
import page (a browser step), the nginx example, an upgrade between two published
images (none is published yet), an external PostgreSQL, and an audit purge.

## Not covered

Kubernetes and other orchestrators: the image and roles work there too (one web
Deployment, one worker Deployment, a migration Job), but this guide describes
Compose only.
