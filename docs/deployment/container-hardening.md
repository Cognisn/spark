# Container Hardening

Every Spark container in the deploy files (`migrate`, `web` and `worker` in
[`deploy/compose.yaml`](compose.md), the [Portainer stack](portainer.md) and the
[Docker Desktop stack](docker-desktop.md)) runs with these controls:

| Control | Compose setting | What it stops |
|---|---|---|
| An unprivileged user | the image's `USER 10001:10001` | A process that is broken into runs as `spark`, not root: it cannot change the image, read other users' files on the host or bind to a privileged port. |
| A read-only root filesystem | `read_only: true` | Nothing can be written into the image's own files: no changed Python package, no planted program or library, no altered certificate bundle. Spark writes only to its volumes and `/tmp` (below). |
| No capabilities | `cap_drop: [ALL]` | The container holds none of the Linux capabilities Docker grants by default (changing file ownership, raw sockets, `setuid` and the rest), so a process inside cannot use them even if it gains root. Spark needs none of them: it listens on port 8000 and touches only files its own user owns. |
| No new privileges | `security_opt: [no-new-privileges:true]` | A process cannot gain privileges through a setuid or setgid program, or through file capabilities, so the unprivileged user stays unprivileged. |
| Docker's default seccomp profile | no `seccomp` option named | The kernel refuses the system calls Docker's default profile blocks (loading kernel modules, mounting file systems, `ptrace` of other processes, changing the clock and others). Spark needs nothing outside it, so no custom profile is shipped; naming `seccomp=unconfined` would remove it. |
| An init process | `init: true` | Signals reach Spark and finished child processes (MCP servers started over stdio) are reaped, so a stop is clean and no zombies build up. |
| `/tmp` as a small tmpfs | `tmpfs: [/tmp:rw,noexec,nosuid,nodev,size=...]` | Scratch files live in memory, vanish when the container stops, and are capped in size; nothing in `/tmp` can be run as a program (`noexec`), carry setuid (`nosuid`) or be a device (`nodev`). |

`tests/deploy/test_container_hardening.py` fails if a Spark service in any of these files
(or in the live end-to-end overlay) loses one of them.

PostgreSQL, the mail catcher and the optional Caddy proxy are third-party images with their
own needs (PostgreSQL's entry point changes ownership of its data directory as root); they
are not changed here. Replace them with managed services for a deployment that must hold
every container to these controls.

## The writable paths

With a read-only root, Spark writes only here:

| Path | Kind | What Spark writes there | Roles |
|---|---|---|---|
| `/var/lib/spark` | volume (`spark-data`) | `HOME` and the XDG directories: `config/spark/config.yaml`, logs (`state/spark/logs`), konfig's encrypted secrets file and its lock, and in personal mode the SQLite database, its local secrets key and bundle exports | all |
| `/var/lib/spark-files` | volume (`spark-files`) | Users' file areas (`files.root`): uploads, documents Spark creates, extracted archives, imported bundles' files and skills, each written through a staging folder on the same volume | web, worker (migrate mounts it but writes nothing) |
| `/var/cache/spark/huggingface` | volume (`spark-models`) | The sentence-transformers embedding model (`HF_HOME`), downloaded on first use, and tiktoken's encoding cache (`TIKTOKEN_CACHE_DIR=/var/cache/spark/huggingface/tiktoken`) | web, worker |
| `/tmp` | tmpfs | Uploads larger than 1 MB while the request is read (Starlette spools them), a bundle being imported (up to 1 GiB), a personal bundle being exported, and torch's `torchinductor_spark` folder, created on import | all |

Sizes of `/tmp`: `web` 2 GiB (a 1 GiB bundle import with room for other uploads at the same
time), `worker` 256 MiB, `migrate` 64 MiB. A tmpfs takes memory only for what is in it, but
counts against the container's memory: raise `files.max_upload_mb` or allow larger bundles
only with a larger `/tmp` and the memory to match.

Docker itself mounts `/etc/hosts`, `/etc/hostname`, `/etc/resolv.conf` and `/dev/shm`; they
stay writable as usual and are not part of the image.

## Checking a running container

From the Docker host, for the Compose project `spark`:

```bash
# The settings Docker applied: true, [ALL], [no-new-privileges:true], true, the tmpfs.
docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.HostConfig.Init}} {{.HostConfig.Tmpfs}}' spark-web-1

# Inside: no capabilities (CapEff and CapBnd 0), no new privileges (1), seccomp filtering (2).
docker exec spark-web-1 grep -E '^(CapEff|CapBnd|NoNewPrivs|Seccomp):' /proc/1/status

# Writing outside the volumes fails; running a program from /tmp fails.
docker exec spark-web-1 sh -c 'touch /opt/venv/x' # Read-only file system
docker exec spark-web-1 sh -c 'cp /bin/true /tmp/t && /tmp/t' # Permission denied

# Nothing in the image's own layer has changed since the container started.
docker diff spark-web-1
```

`docker diff` lists changes to the container's own layer. With a read-only root it prints
only the mount points Docker itself creates: `/usr/sbin/docker-init` for the init process
(with its parents `/usr` and `/usr/sbin` shown as changed), and the target of any bind mount
you add. Volumes and the tmpfs are not part of the layer.

## ECS on Fargate

This section is the reference for the controls on ECS with the Fargate launch type: what
each task definition, service and `run-task` must carry. [Running on AWS](aws.md) shows them
in place, in complete task definitions for the web, worker and migrate tasks; keep the two in
step by changing this section first.

Fargate does not support `linuxParameters.tmpfs`, so `/tmp` is a task volume with no `host`
(a bind mount on the task's ephemeral storage, never a host path or EFS) and no size of its
own; it shares the task's ephemeral storage (20 GiB by default). The image declares `/tmp` as
a `VOLUME`, which makes Fargate give that volume the image's `/tmp` mode (`1777`), so the
unprivileged user can write to it.

**Every Spark container** (web, worker, and migrate and bootstrap) adds these settings to its
container definition, beside its own image, environment, health check and logging, and adds
the `/tmp` mount to its own `mountPoints`:

```json
{
  "user": "10001:10001",
  "readonlyRootFilesystem": true,
  "privileged": false,
  "linuxParameters": {
    "capabilities": { "drop": ["ALL"] },
    "initProcessEnabled": true
  },
  "mountPoints": [
    { "sourceVolume": "spark-tmp", "containerPath": "/tmp", "readOnly": false }
  ]
}
```

Add no capabilities (`capabilities.add` stays empty), and use no host path for any volume.

**Every task definition** adds the `/tmp` volume to its `volumes`, beside its EFS volumes,
which stay as they are (web and worker mount all three, migrate `spark-data` only):

```json
{ "name": "spark-tmp" }
```

**No shell access into running tasks.** Create each service (web and worker) with ECS Exec
off, and start every one-off task (migrate and bootstrap) with it off:

```json
{ "serviceName": "<spark-web>", "enableExecuteCommand": false }
```

```bash
aws ecs run-task ... --no-enable-execute-command
```

In a CloudFormation template that is `EnableExecuteCommand: false` on each
`AWS::ECS::Service`. ECS Exec is off by default; set it explicitly so a later change has to be
deliberate, and grant no task role `ssmmessages:*` (the channel ECS Exec uses), so it could
not work even if switched on.

The checks above (`docker inspect`, `docker exec`) are for Compose hosts. On Fargate, read
the settings with `aws ecs describe-task-definition` and `aws ecs describe-services`
(`enableExecuteCommand`). The test environment's deploy does this before every change
([Deploying to the test environment](aws.md#the-hardening-check)).

## How this was checked

On Spark 0.3.0rc37, a stack built from this repository's `Dockerfile` with
`deploy/compose.yaml` and the live end-to-end overlay (`deploy/live/compose.live.yaml`: two
web replicas, an active and a standby worker, an MCP server, a scripted model server and an
OIDC provider), every Spark container under the controls above: first start, migrations and
bootstrap; the administrator's invitation and sign-in with two-step verification and single
sign-on; chats through the model stub; uploads (one of 8 MB) and reading a Word document
from the file area; an archive extracted into the file area; a memory
embedded, stored and found by search (the embedding model loaded); an MCP tool call; a
knowledge graph build and a scheduled action run by the worker; worker failover; the health
check and `/ready`; a restart of every Spark container, with the checks run again after it.
Personal mode (`spark web` with no database settings) was started the same way and wrote a
bundle with `spark export`. `docker diff` on every Spark container showed no change
to the image's layer. ECS on Fargate was not run: the settings above are written from AWS's
documentation and have not been applied to an AWS account.
