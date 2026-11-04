# Docker Desktop

`deploy/docker-desktop/compose.yaml` runs a complete Spark server on your own
computer with Docker Desktop (Windows, macOS or Linux): PostgreSQL with pgvector, a
one-shot migration, the web role and the worker role. It is for trying server mode
(users, roles, budgets, the worker) on one machine. Every port binds to `127.0.0.1`,
so nothing is reachable from another computer.

For a server other people use, deploy on a server host with a TLS reverse proxy:
see [Compose deployment](compose.md), or [Portainer](portainer.md) if you manage
the host with Portainer. For Spark as a personal app with no server at all, see
[Getting Started](../getting-started.md).

## What you need

- Docker Desktop with Docker Compose 2.24 or later (`docker compose version`).
- At least 4 GB of memory for Docker Desktop (Settings > Resources); 6 GB is
  comfortable once chats and knowledge graphs load the embedding model. About 4 GB
  of disk for the image, the database and the model.
- Outbound https the first time someone chats or searches memories: the embedding
  model (about 90 MB) is downloaded from Hugging Face once.
- An authenticator app for two-step verification, which every Spark server account
  sets up at its first sign-in.
- Chrome, Edge or Firefox for the plain `http://localhost` address. Spark's sign-in
  cookies are marked `Secure`; these browsers keep `Secure` cookies for `localhost`
  over plain http, Safari does not. With Safari, use [the https
  profile](#https-on-localhost).
- An API key for at least one model provider (Anthropic, Google Gemini, X.AI or AWS
  Bedrock), or an Ollama server, to chat.

## Get the files

Make a folder and put two files from the repository's `deploy/docker-desktop/`
folder in it: `compose.yaml` and `spark.env.example`. A checkout of the repository
works too: run the commands below from `deploy/docker-desktop`.

On Windows, run the commands in PowerShell or Windows Terminal.

## The image

Release images are on `ghcr.io/cognisn/spark`, tagged with the version only
(`0.3.0rc1`, `0.3.0`; there is no `latest`):

```bash
docker pull ghcr.io/cognisn/spark:0.3.0rc1
```

To build it yourself instead, from a checkout of the repository:

```bash
docker build -t spark:0.3.0rc1 .
```

and use `spark:0.3.0rc1` as `SPARK_IMAGE` below.

## Write the env file

Copy `spark.env.example` to `spark.env` in the same folder and replace every value
ending in `-dev-only`:

| Variable | Purpose |
|---|---|
| `SPARK_IMAGE` | The image and version, for example `ghcr.io/cognisn/spark:0.3.0rc1` |
| `POSTGRES_PASSWORD` | The database owner's password. Spark's database URL is built from it, so use letters, digits, `-` and `_` only |
| `POSTGRES_USER`, `POSTGRES_DB` | The database owner and database name (default `spark` for both) |
| `KONFIG_MASTER_KEY` | The key to konfig's encrypted secrets file, which holds the provider keys you enter and warden's signing keys |
| `SPARK_PUBLIC_BASE_URL` | The address you open in the browser, exactly (default `http://localhost:8000`) |
| `SPARK_WEB_PORT` | The port Spark is published on, on `127.0.0.1` (default `8000`) |

Generate the master key with the image itself:

```bash
docker run --rm --entrypoint python ghcr.io/cognisn/spark:0.3.0rc1 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep `spark.env` private, and keep a copy of `KONFIG_MASTER_KEY`: without it the
secrets file cannot be read, and every provider key and sign-in key in it is lost.
The file may have Windows line endings.

If port 8000 is taken, change `SPARK_WEB_PORT` and the port in
`SPARK_PUBLIC_BASE_URL` together, for example `8080` and `http://localhost:8080`.

## Start it

```bash
docker compose --env-file spark.env -f compose.yaml up -d
docker compose --env-file spark.env -f compose.yaml ps -a
```

The project is named `spark-desktop`, and Docker Desktop's Containers view shows it
under that name. When the stack is up, `migrate` shows `Exited (0)` (it brings the
database to the current schema at each start, then exits), `postgres` and `web`
show `healthy`, and `worker` shows `Up`. On a first start this takes under a
minute once the images are pulled. If a variable is missing, Compose refuses to start and
names it.

## The first administrator

A new server has no users. Create the first administrator; it asks for a password
twice (at least 12 characters):

```bash
docker compose --env-file spark.env -f compose.yaml exec web spark identity create-admin --email admin@example.com
```

It prints `administrator <id>, created, administrator role assigned`. Use your own
address: it is the account's sign-in name, and the address password resets go to.

Open `http://localhost:8000` (or your `SPARK_PUBLIC_BASE_URL`), sign in, and scan
the QR code with your authenticator app to finish two-step verification. Keep the
recovery codes it shows.

Then, on the instance settings page:

1. Enable a provider and enter its API key. The key is stored in konfig's encrypted
   secrets file, never in the database.
2. Choose the models offered to users.
3. Optionally set up mail (`mail.smtp`). Without it, Spark shows an administrator
   each invitation and password reset link once, to pass on yourself; see
   [Identity](identity.md#mail).

[Identity](identity.md) covers roles, invitations, budgets and single sign-on. To
try a second user on the same computer, invite one from the administration screens
and accept the invitation in a private window or another browser profile.

## https on localhost

The `https` profile adds Caddy in front of Spark on `https://localhost:8443`, with a
certificate from Caddy's own local authority. Use it with Safari, or to try Spark
over https as users would see it.

1. In `spark.env`, set `SPARK_PUBLIC_BASE_URL=https://localhost:8443`.
2. Start the stack with the profile:

   ```bash
   docker compose --env-file spark.env -f compose.yaml --profile https up -d
   ```

3. Open `https://localhost:8443`. The browser warns that the certificate is not
   trusted until you trust Caddy's root certificate. To trust it, copy it out and
   add it to your system's trusted roots (Keychain Access on macOS, "Trusted Root
   Certification Authorities" in the Windows certificate manager):

   ```bash
   docker compose --env-file spark.env -f compose.yaml --profile https cp https:/data/caddy/pki/authorities/local/root.crt caddy-root.crt
   ```

Port 8443 is fixed in the Compose file. Changing the public address ends everyone's
sessions, and the address you open must then be the https one: `http://localhost:8000`
still answers, but signing in there is refused by the Origin check. Every later
command needs `--profile https` too, or the proxy is left out.

## Stopping, upgrading and removing

```bash
docker compose --env-file spark.env -f compose.yaml stop
docker compose --env-file spark.env -f compose.yaml up -d
```

`stop` keeps everything; `up -d` starts it again. Docker Desktop also starts the stack
again when it starts, until you stop it.

To upgrade, read the release notes ([CHANGELOG.md](../../CHANGELOG.md)), take a
[backup](#backups), set the new version in `SPARK_IMAGE`, and run `up -d`. The
`migrate` service upgrades the database before web and worker start. Going back to
an older version needs the backup: see [Backups and upgrades](operations.md).

To remove the stack:

```bash
docker compose --env-file spark.env -f compose.yaml down
```

`down` keeps the volumes, and with them every user's data. `down -v` **deletes the
volumes**: the database, the secrets file and every uploaded file.

## Backups

The state is in the `postgres-data`, `spark-data` and `spark-files` volumes, plus
`KONFIG_MASTER_KEY` in `spark.env`. These commands write each copy inside its
container and copy it out, which works the same in PowerShell, Terminal and a
Linux shell:

```bash
docker compose --env-file spark.env -f compose.yaml exec postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom -f /tmp/spark-db.dump'
docker compose --env-file spark.env -f compose.yaml cp postgres:/tmp/spark-db.dump spark-db.dump
docker compose --env-file spark.env -f compose.yaml exec web tar -C /var/lib/spark -cf /tmp/spark-data.tar .
docker compose --env-file spark.env -f compose.yaml cp web:/tmp/spark-data.tar spark-data.tar
docker compose --env-file spark.env -f compose.yaml exec web tar -C /var/lib/spark-files -cf /tmp/spark-files.tar .
docker compose --env-file spark.env -f compose.yaml cp web:/tmp/spark-files.tar spark-files.tar
```

`spark-data.tar` holds the encrypted secrets and the dump holds every conversation,
so keep them somewhere private. [Backups and upgrades](operations.md#restoring)
describes restoring. Its commands feed each file on standard input, which
PowerShell can alter; on Windows, copy the file into the container first
(`docker compose --env-file spark.env -f compose.yaml cp spark-db.dump postgres:/tmp/spark-db.dump`)
and give the restore command the path (`pg_restore ... /tmp/spark-db.dump`,
`tar -xf /tmp/spark-data.tar`) instead.

## When something goes wrong

| What you see | Why, and what to do |
|---|---|
| "Request refused: it did not come from this Spark instance's own pages (Origin check failed)" | The address in the browser differs from `SPARK_PUBLIC_BASE_URL`. `http://127.0.0.1:8000` and `http://localhost:8000` are different addresses: open exactly the configured one. |
| Signing in at `http://localhost` fails or returns to the sign-in page | The browser refused the `Secure` sign-in cookies over plain http (Safari does). Use [the https profile](#https-on-localhost). |
| `required variable ... is missing a value` | `spark.env` lacks that variable, or the command was run without `--env-file spark.env`. |
| `migrate` shows `Exited (1)` | `docker compose --env-file spark.env -f compose.yaml logs migrate` names the problem; web and worker wait for it. |
| `web` never becomes healthy | `docker compose --env-file spark.env -f compose.yaml logs web`. A setting Spark cannot read stops it at start-up with a message naming the setting. |
| "port is already allocated" | Another program uses the port: change `SPARK_WEB_PORT` and `SPARK_PUBLIC_BASE_URL` together. |
| Everything is slow or containers restart | Give Docker Desktop more memory (Settings > Resources). |

> Checked for this guide: the Compose file run as written with Docker Compose
> 5.5.1 against an image built from this repository, with an env file saved with
> Windows line endings and the master key generated by the command above. Then:
> first start, the first administrator from `exec`, a password sign-in at
> `http://localhost` through two-step enrolment, the Origin refusal for
> `http://127.0.0.1`, the worker holding its lock, the https profile with Caddy's
> root certificate (the sign-in page and a password sign-in through to the
> second-factor step) and the backup commands. The project name and ports were
> changed to fit the test machine. Not run: Docker Desktop's own interface, Safari,
> and trusting the root certificate in a system store.
