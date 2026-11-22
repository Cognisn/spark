# Running Spark on AWS (ECS on Fargate)

This page runs a Spark server on Amazon ECS with the Fargate launch type, with Amazon RDS for
PostgreSQL, Amazon EFS, AWS Secrets Manager and Amazon Bedrock. It describes one layout in
full, with example values only (account `123456789012`, `example.com`, `CHANGEME`).

> **What was checked.** Spark's tests run konfig's real settings document and secrets bundle
> against a stubbed Secrets Manager, prove that every connection Spark opens finds the RDS
> certificate authority bundle through `PGSSLROOTCERT` and verifies the server with
> `sslmode=verify-full` (against a stand-in server with a test authority), build a Bedrock
> client from boto3's default credential chain with no keys, and exercise `/health` and
> `/ready`. None of it called AWS. The task definitions, policies and sizes below are
> written from the settings those tests use and have not been run against an AWS account:
> try them in a test environment first.

## The layout

```
 users --- https --> Application Load Balancer (ends TLS, health check /health)
                              | plain HTTP, port 8000 only
                     +--------+---------+
                     |                  |
              ECS service           ECS service              one-off task
              spark-web             spark-worker             migrate + bootstrap
              (Fargate)             (Fargate, one active)    (Fargate, run before the services)
                     |                  |                          |
        RDS for PostgreSQL 16 (pgvector 0.8, TLS required) <-------+
        EFS: three access points         Secrets Manager: bundle + settings document
        Bedrock: through the task role
```

- Tasks run in private subnets with outbound access through a NAT gateway (to Secrets
  Manager, Bedrock, the image registry and the embedding model download). The load balancer's
  security group is the only source allowed to reach the web task's port 8000.
- Fargate platform version 1.4.0 or later (EFS), x86_64.
- The web and worker roles use the same image; see [Compose deployment](compose.md) for what
  each role does.

## Configuration: the settings document and the bundle

Spark reads its start-up settings through konfig. On AWS two Secrets Manager secrets carry
everything, and a task's environment names them:

| Variable | Value |
|---|---|
| `KONFIG_AWS_SETTINGS` | The ARN of the **settings document**: one secret whose value is a JSON object of non-secret settings. konfig only reads it. |
| `KONFIG_AWS_SECRETS_MANAGER` | The ARN of the **bundle**: one secret whose value is a JSON object of secrets. Spark reads it, and the web and migrate tasks write to it (see [Secrets](secrets.md#on-aws-secrets-manager)). |
| `KONFIG_AWS_SEED` | `false`. konfig then never writes placeholders into an empty store, so the two secrets are exactly what you filled in. |

There is no `KONFIG_MASTER_KEY`: the encrypted file store is not used, and server mode does
not ask for a key when the bundle is the store.

### The settings document

Keep it small. Anything the administration screens can change is stored in the database and
does not belong here.

```json
{
  "mode": "server",
  "server": { "public_base_url": "https://spark.example.com" },
  "files": { "root": "/var/lib/spark-files" },
  "logging": { "level": "INFO", "format": "json" }
}
```

Precedence, highest first: a `SPARK__...` environment variable, the settings document,
`config.yaml`, Spark's defaults. So `SPARK__LOGGING__LEVEL=DEBUG` on one task overrides the
document for that task, and the document overrides a `config.yaml` on the EFS volume. The
tests check this for each key above (`mode`, `server.public_base_url`, `files.root`,
`logging.level` and `logging.format`).

`server.public_base_url` is the https address of the load balancer's DNS name (see
[Reverse proxy and TLS](reverse-proxy.md): the Origin check and the sign-in cookies need it
exactly).

### The bundle and one database login per task

The bundle holds one database URL per login, each with `sslmode=verify-full` and no
`sslrootcert` (the certificate authority bundle comes from `PGSSLROOTCERT`, below):

```json
{
  "database_url_owner": "postgresql://spark:CHANGEME@db.example.com:5432/spark?sslmode=verify-full",
  "database_url_web": "postgresql://spark_web:CHANGEME@db.example.com:5432/spark?sslmode=verify-full",
  "database_url_worker": "postgresql://spark_worker:CHANGEME@db.example.com:5432/spark?sslmode=verify-full"
}
```

Each task points `SPARK__DATABASE__URL` at its own login:

| Task | `SPARK__DATABASE__URL` | Login |
|---|---|---|
| migrate and bootstrap | `secret://database_url_owner` | the schema owner (creates and changes the schema) |
| web | `secret://database_url_web` | a restricted login that owns nothing |
| worker | `secret://database_url_worker` | a restricted login that owns nothing |

The reference is resolved through the bundle for `spark web`, `spark worker` and
`spark migrate` alike, and a `SPARK__DATABASE__URL` in the environment wins over any
`database.url` in the settings document or `config.yaml`. The value is a reference, not a
URL, so the task definition carries no password. The restricted logins and their grants are
those in [Database](database.md#restricted-roles); the headless bootstrap creates them.

Fill in both secrets before the first start. With `KONFIG_AWS_SEED=false` an empty bundle
stays empty and Spark refuses to start with "database.url refers to the secret ... which is
not set in the secrets store" (the message names the secret, never a value). Spark adds
warden's signing keys and its other generated secrets to the bundle when `spark bootstrap` first runs
([Identity](identity.md#bootstrap)).

## TLS to RDS

RDS requires TLS (`rds.force_ssl`), and `sslmode=verify-full` checks both the certificate
chain and the host name. The image carries the AWS RDS certificate authority bundle at
exactly:

```
/etc/ssl/certs/rds-global-bundle.pem
```

and every task sets:

```
PGSSLROOTCERT=/etc/ssl/certs/rds-global-bundle.pem
```

libpq (and so every connection Spark opens) reads that variable when the URL has no
`sslrootcert`. Spark's tests check that its pools (the web server's, warden's, the worker's
and the audit purge's), the worker's lock connection, the settings listener, `spark migrate`
and warden's migrations all verify the server against the bundle named by `PGSSLROOTCERT`,
refuse a server whose certificate it does not sign, and never fall back to a weaker mode when
the variable is unset or names a missing file. If the variable is missing, connections fail;
they do not quietly use `sslmode=require`.

The bundle is public certificates only, fetched when the image is built from
`https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem` and checked against a
SHA-256 pinned in the `Dockerfile` (`RDS_CA_BUNDLE_SHA256`); a changed download fails the
build. AWS republishes the bundle when it adds an authority: check the new file and pass the
new checksum as a build argument. `deploy/check-image.sh` checks the file is present, readable
by the image's user (uid 10001) and holds no private key. The bundle covers the
`rds-ca-rsa2048-g1` authority and the others AWS lists for RDS.

## EFS

Create one file system with three access points, each with POSIX user and group
`10001:10001` (the image's `spark` user) and its own root directory and permissions (for
example `0750`):

| Access point | Mount path in the task | Used by |
|---|---|---|
| `/spark-data` | `/var/lib/spark` | web, worker, migrate (configuration and logs) |
| `/spark-files` | `/var/lib/spark-files` | web, worker (users' file areas; `files.root`) |
| `/hf-cache` | `/var/cache/spark/huggingface` | web, worker (the embedding model download, shared) |

Turn on transit encryption and IAM authorisation on every volume. The file system's
security group allows NFS (2049) from the tasks' security group only. The first start of web
or worker downloads the embedding model into the cache, so allow outbound access to the model
host and a generous health check start period.

## Task definitions

Three task definitions from one image, `123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/spark:0.3.0`
(example). Fargate ignores the image's `HEALTHCHECK`, so the web task declares its own.

Every Spark container carries the container hardening controls: user `10001:10001`, a
read-only root filesystem, no privilege, every Linux capability dropped, an init process, and
`/tmp` on a task volume (`spark-tmp`) of the task's own ephemeral storage. [Container
hardening](container-hardening.md#ecs-on-fargate) is the reference for these settings and
what each stops; the task definitions below carry them in place.

### Web

```json
{
  "family": "spark-web",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "runtimePlatform": { "cpuArchitecture": "X86_64", "operatingSystemFamily": "LINUX" },
  "cpu": "2048",
  "memory": "8192",
  "executionRoleArn": "arn:aws:iam::123456789012:role/spark-execution",
  "taskRoleArn": "arn:aws:iam::123456789012:role/spark-web-task",
  "containerDefinitions": [
    {
      "name": "web",
      "image": "123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/spark:0.3.0",
      "essential": true,
      "user": "10001:10001",
      "readonlyRootFilesystem": true,
      "privileged": false,
      "linuxParameters": {
        "capabilities": { "drop": ["ALL"] },
        "initProcessEnabled": true
      },
      "portMappings": [{ "containerPort": 8000, "protocol": "tcp" }],
      "environment": [
        { "name": "KONFIG_AWS_SETTINGS", "value": "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:spark/settings-AbCdEf" },
        { "name": "KONFIG_AWS_SECRETS_MANAGER", "value": "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:spark/secrets-GhIjKl" },
        { "name": "KONFIG_AWS_SEED", "value": "false" },
        { "name": "SPARK__DATABASE__URL", "value": "secret://database_url_web" },
        { "name": "PGSSLROOTCERT", "value": "/etc/ssl/certs/rds-global-bundle.pem" }
      ],
      "mountPoints": [
        { "sourceVolume": "spark-data", "containerPath": "/var/lib/spark" },
        { "sourceVolume": "spark-files", "containerPath": "/var/lib/spark-files" },
        { "sourceVolume": "hf-cache", "containerPath": "/var/cache/spark/huggingface" },
        { "sourceVolume": "spark-tmp", "containerPath": "/tmp", "readOnly": false }
      ],
      "healthCheck": {
        "command": ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)\""],
        "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 120
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": { "awslogs-group": "/ecs/spark-web", "awslogs-region": "ap-southeast-2", "awslogs-stream-prefix": "web" }
      }
    }
  ],
  "volumes": [
    { "name": "spark-data", "efsVolumeConfiguration": { "fileSystemId": "fs-0123456789abcdef0", "transitEncryption": "ENABLED", "authorizationConfig": { "accessPointId": "fsap-0123456789abcdef0", "iam": "ENABLED" } } },
    { "name": "spark-files", "efsVolumeConfiguration": { "fileSystemId": "fs-0123456789abcdef0", "transitEncryption": "ENABLED", "authorizationConfig": { "accessPointId": "fsap-0123456789abcdef1", "iam": "ENABLED" } } },
    { "name": "hf-cache", "efsVolumeConfiguration": { "fileSystemId": "fs-0123456789abcdef0", "transitEncryption": "ENABLED", "authorizationConfig": { "accessPointId": "fsap-0123456789abcdef2", "iam": "ENABLED" } } },
    { "name": "spark-tmp" }
  ]
}
```

Size memory for the embedding model and your users; 8 GiB is a starting point, not a
measurement. `/tmp` takes its room from the task's ephemeral storage (20 GiB by default), which
holds a bundle being imported (up to 1 GiB) and large uploads while they are read. The execution role is the standard ECS task execution role (image pull and log
delivery). Nothing in the task definition is a secret: the database URL is a reference and
the credentials come from the task role.

### Worker

The same as the web task with these differences: family `spark-worker`, task role
`spark-worker-task`, `SPARK__DATABASE__URL` set to `secret://database_url_worker`, the
container `command` set to `["worker"]` (the image's entry point is `spark`), no port
mapping and no `healthCheck`. Everything else is as in the web task, the hardening settings,
the three EFS volumes and the `spark-tmp` volume at `/tmp` included. Run one task or more: one is active at a time (it holds a
PostgreSQL advisory lock) and the others stand by. Start the worker service after the web
service is healthy, or at least after the migrate and bootstrap task: the worker's role cannot
write the bundle, and bootstrap creates warden's signing keys in it (a worker started earlier
waits for them).

### Migrate and bootstrap (one-off)

Run this task to completion before the web and worker services, and again for each upgrade:

- family `spark-migrate`, task role `spark-migrate-task`, `SPARK__DATABASE__URL` set to
  `secret://database_url_owner`, the same `KONFIG_...` and `PGSSLROOTCERT` variables;
- `entryPoint` `["/bin/sh", "-c"]` and `command` `["spark migrate && spark bootstrap"]`, which
  brings both schemas (Spark's and warden's) to head and then runs the headless bootstrap
  (which creates the restricted logins `spark_web` and `spark_worker`);
- the same hardening settings as the web container (`user`, `readonlyRootFilesystem`,
  `privileged`, `linuxParameters`);
- mount `/spark-data` and the `spark-tmp` volume at `/tmp` only (volumes `spark-data` and
  `spark-tmp`); no port, no health check. Its `/bin/sh -c` entry point needs nothing writable
  beyond `/tmp`.

Run it with `aws ecs run-task` on the same cluster, subnets and security group as the
services, with ECS Exec off, and check that the task's container exit code is 0 before
starting the services. `spark migrate` is safe to repeat:

```bash
aws ecs run-task --cluster spark --task-definition spark-migrate --launch-type FARGATE \
  --count 1 --no-enable-execute-command \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-0123456789abcdef0],securityGroups=[sg-0123456789abcdef0],assignPublicIp=DISABLED}"
```

### The services and ECS Exec

Create the `spark-web` and `spark-worker` services with ECS Exec off, set explicitly so that
turning it on is a deliberate change (in CloudFormation, `EnableExecuteCommand: false` on each
`AWS::ECS::Service`):

```json
{ "serviceName": "spark-web", "enableExecuteCommand": false }
```

With ECS Exec off and no `ssmmessages` permission on any task role (below), nobody opens a
shell in a running task. Read the settings back with `aws ecs describe-services` (the
services' `enableExecuteCommand`) and `aws ecs describe-task-definition`.

## The load balancer

- Target group: protocol HTTP, port 8000, target type `ip`, health check path `/health`,
  success code 200, interval 30 s. Add `deregistration_delay` of 30 s.
- Listener: HTTPS 443 with your certificate, forwarding to the target group. The security
  group allows 443 from your users and 8000 from the load balancer to the web tasks only.
- Raise the idle timeout above the default 60 s if you use long streaming chats (streams
  send data continuously; 120 s is a sensible margin).

### `/health` and `/ready`

Both answer without a sign-in and outside warden's checks, so a health check never needs a
session or an API key.

| Path | 200 when | 503 when | Use for |
|---|---|---|---|
| `/health` | the process is serving; it depends on nothing | never (the process is down instead) | the load balancer's target check and the container health check |
| `/ready` | the database answers, the schema is at both heads (Spark's and warden's) and the secrets store is readable | any of those fails | a deployment gate, a synthetic monitor, and diagnosing a task that is healthy but not useful |

`/ready` returns JSON, for example:

```json
{ "status": "ready", "version": "0.3.0", "checks": { "database": "ok", "schema": "ok", "secrets": "ok" } }
```

A failing check reads `"failed"`. The body carries no secret, URL, host name or stack trace;
the cause is in the task's log (the exception's type, never its text). Use `/health` for the
load balancer, not `/ready`: a database outage would then take every web task out of service
at once, and replacing tasks does not fix a database.

## Bedrock through the task role

A Bedrock model connection (Administration, Connections) with authentication `credential_chain`
holds no key and no `secret://` reference. Spark builds the Bedrock client through boto3's
default credential chain, which on ECS finds the task role's credentials from the container
credentials endpoint. Choose **credential chain** and leave the profile empty; do not put an
access key anywhere. The connection's region should match where the models are enabled.

The task role needs, on the models and inference profiles the connection uses:

- `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` (chat, streaming and
  the worker's knowledge graph builds);
- `bedrock:ListFoundationModels` and `bedrock:ListInferenceProfiles` (the model list on the
  Connections page; `Resource: "*"`, these actions do not support narrower resources).

Enable model access for the account in the Bedrock console, and use an inference profile ARN
where the model needs one. Both the web and the worker task roles need the invoke permissions:
the web task answers chat, and the worker runs scheduled actions and knowledge graph builds.
The list permissions matter to the web task only.

### Adding the connection at deployment

A fresh deployment has no model connection until an administrator adds one. To have
`spark bootstrap` add the Bedrock connection above, set these on the stack (the migrate and
bootstrap task, or the settings document). They are read by `spark bootstrap` in server mode
only; personal mode has no bootstrap, so it ignores them.

| Setting | Variable | Meaning |
|---|---|---|
| `bootstrap.bedrock.region` | `SPARK__BOOTSTRAP__BEDROCK__REGION` | An AWS Region, for example `ap-southeast-2`. Set, it turns the step on; unset or empty, nothing happens |
| `bootstrap.bedrock.name` | `SPARK__BOOTSTRAP__BEDROCK__NAME` | Optional display name (default Amazon Bedrock (task role)) |
| `bootstrap.bedrock.residency_regions` | `SPARK__BOOTSTRAP__BEDROCK__RESIDENCY_REGIONS` | Optional, comma-separated Regions (a JSON list in a settings document also works), for example `ap-southeast-2,ap-southeast-4`. It must include the Region above |
| `bootstrap.bedrock.models` | `SPARK__BOOTSTRAP__BEDROCK__MODELS` | Optional, comma-separated model ids (a JSON list in a settings document also works), for example `amazon.nova-lite-v1:0,au.anthropic.claude-sonnet-4-6`. Set, the connection bootstrap creates offers exactly those models at the Standard tier and every other model is off until an administrator switches it on. Ids are checked for form only; one the service does not list, or conduit refuses under the residency policy, stays stored and is not offered. Never applied to a connection that exists |

What is created is one connection, `aws-bedrock`, service Amazon Bedrock, enabled, with
authentication credential chain, no profile and no secret reference. A Region or name
that is malformed stops `spark bootstrap` before anything is written, naming the variable. A
Region that is well formed but that Amazon Bedrock's runtime endpoint does not serve is refused
when the connection is validated, with conduit's reason in the error, and nothing is stored.

**Residency.** By default a Bedrock connection allows processing in its own Region only. In
`ap-southeast-2` Anthropic models are offered only through the AU geographic inference
profiles (`au.anthropic.*`), which may be processed in `ap-southeast-2` and `ap-southeast-4`
(some older ones also in `ap-southeast-6`). The default refuses every one of them, so a
connection bootstrapped without `residency_regions` can reach no Claude model in that Region.
With `residency_regions` set, the connection allows local, in-Region and cross-Region
processing within exactly those Regions, and never global. A profile whose Regions are not
all listed is refused: with `ap-southeast-2,ap-southeast-4`, the profiles that also list
`ap-southeast-6` stay refused (shown with their reason on the Connections page) until
`ap-southeast-6` is allowed there by an administrator.

### Choosing Regions for the Bedrock connection

An administrator chooses, for each Bedrock connection, the Regions its requests may be
processed in. On the Connections page, choose **Regions** on the connection, tick the AWS
Regions and, if the connection may also use `global.` inference profiles (which any supported
AWS commercial Region may process), tick **Also allow global processing**. The list offered is
the set of Regions where the `bedrock-runtime` endpoint is available; the connection's own
Region must be among those chosen. Spark saves the choice with the connection (its Bedrock
`processing` setting), records the change in the audit log by field name only, and refuses a
choice conduit rejects, showing its reason, without saving anything.

The choice only narrows. A model is usable only when the deployment's residency policy and the
connection's Region choice both allow it, so a connection that chooses no Regions behaves as it
always did. Choosing `ap-southeast-2` alone, for example, refuses an AU cross-Region profile
that may also be processed in `ap-southeast-4`, and the Connections page shows that reason.
`bootstrap.bedrock.residency_regions` sets the same choice (the listed Regions, without
global) together with the residency policy when the connection is first created; a connection
that already exists is never changed by bootstrap.

**Where each model is processed** comes from the service's own listing, not from a built-in
table. For every model Bedrock lists, the Connections page shows the Regions it is processed
in, whether it is usable under the connection's residency, and the reason when it is refused.
Differences between Spark's catalogue and the listing (a model with no capability record, a
catalogued model Bedrock does not list, different limits or capabilities) are shown as
information; they never hide a model. A model with no capability record is shown under
"Reachable but not in the catalogue", and is declared there before use.

**Once only.** Bootstrap adds the connection when none with that id exists and it has not
dealt with it before, and then records that it has. It never changes, re-enables or
overwrites a connection that exists, whatever its state, Region or residency, and it does not
add it again if an administrator deleted it. An instance that was bootstrapped before this
setting existed gets the connection on its next run. Running bootstrap again is safe.

**Models.** The connection stores no model rows. A connection with none offers every model in
its Region that residency allows, enabled at the standard tier, so no administrator step is
needed before people can chat; an administrator who wants to curate the list opens the
connection on the Connections page. Task role permissions and model access in the Bedrock
console still apply.

## IAM: one role per task

Each task has its own task role. Nothing grants `secretsmanager:*` or `*` anywhere, and no
task role grants any `ssmmessages:` action (the channel ECS Exec opens a shell through), so a
task cannot be entered even if ECS Exec were turned on. The examples use example ARNs.

**Web task role** (`spark-web-task`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SecretsBundleReadWrite",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
      "Resource": "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:spark/secrets-GhIjKl"
    },
    {
      "Sid": "SettingsDocumentRead",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:ap-southeast-2:123456789012:secret:spark/settings-AbCdEf"
    },
    {
      "Sid": "EfsMountWrite",
      "Effect": "Allow",
      "Action": ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"],
      "Resource": "arn:aws:elasticfilesystem:ap-southeast-2:123456789012:file-system/fs-0123456789abcdef0",
      "Condition": {
        "StringEquals": {
          "elasticfilesystem:AccessPointArn": [
            "arn:aws:elasticfilesystem:ap-southeast-2:123456789012:access-point/fsap-0123456789abcdef0",
            "arn:aws:elasticfilesystem:ap-southeast-2:123456789012:access-point/fsap-0123456789abcdef1",
            "arn:aws:elasticfilesystem:ap-southeast-2:123456789012:access-point/fsap-0123456789abcdef2"
          ]
        }
      }
    },
    {
      "Sid": "BedrockInvoke",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
      "Resource": [
        "arn:aws:bedrock:ap-southeast-2::foundation-model/*",
        "arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/*"
      ]
    },
    {
      "Sid": "BedrockList",
      "Effect": "Allow",
      "Action": ["bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles"],
      "Resource": "*"
    }
  ]
}
```

Narrow `foundation-model/*` to the models you enable. If the secrets are encrypted with a
customer managed KMS key, also allow `kms:Decrypt` (and `kms:GenerateDataKey` for the roles
that write) on that key.

**Worker task role** (`spark-worker-task`): the same, except the bundle statement allows
`secretsmanager:GetSecretValue` only (it never writes), and the EFS statement may list only
the access points it mounts.

**Migrate and bootstrap task role** (`spark-migrate-task`): the web role's two Secrets
Manager statements, and `ClientMount` and `ClientWrite` on the `/spark-data` access point
only. No Bedrock, no other EFS access point.

The **execution role** is AWS's `AmazonECSTaskExecutionRolePolicy`, with `kms:Decrypt` added
only if your log group or registry needs it.

## Order of a first deployment

1. Create the RDS instance (PostgreSQL 16, pgvector 0.8, `rds.force_ssl`), EFS, the two
   secrets (filled in), the roles, the log groups, the load balancer and the services at a
   desired count of 0.
2. Run the migrate and bootstrap task and wait for exit code 0.
3. Scale `spark-web` to 1, and wait for `/health` (the first start downloads the embedding
   model).
4. Call `/ready` from inside the network (or read the task's log): all three checks `ok`.
5. Scale `spark-worker` up.
6. Follow the invitation that `spark bootstrap` mailed to `bootstrap.admin.email`
   ([the first administrator](identity.md#the-first-administrator)), sign in, and
   [add a Bedrock connection](../connections.md). `spark identity create-admin` is only
   for [recovery](identity.md#restoring-an-administrator).

## Upgrades

Run the migrate task with the new image first, then update both services to the new task
definition revision. Spark's web start refuses a database that is not at its head (see
[Backups and upgrades](operations.md#upgrades)), so a task started before the migration
stops with a message naming `spark migrate`, and a database moved out from under a running
task shows `"schema": "failed"` on `/ready`.

## Deploying to the test environment

A release-candidate tag (`v0.3.0rcN`) deploys itself to the test environment at
`https://test.spark.cognisn.net`, with no manual step. The `deploy-test` job of
`.github/workflows/container-publish.yml` runs after the image is published to ghcr.io and the
`publish-central` job has copied it to the central repository and signed it
([Release images](release-images.md)), only for a tag push whose version is a release candidate
(never a final version, never a manual dispatch),
in the GitHub environment `spark-test`, one deploy at a time (a second waits, it is not
cancelled). It signs in to AWS with GitHub's OIDC token, so the repository holds no AWS secret.

The repository is public, so its run logs are public: the job masks every AWS identifier before
a step can print one, and prints and summarises none. The identifiers live only in the
environment's variables: `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `ECR_REPOSITORY_URI`,
`SPARK_TEST_STACK_NAME` (the service stack), `SPARK_TEST_SERVICE_ROLE_NAME` (the
CloudFormation execution role the change sets run as), `CENTRAL_ECR_REPOSITORY_URI` (the central
repository, without a tag) and `IMAGE_SIGNING_PUBLIC_KEY` (the release key's public key, PEM; it
is not secret, and every `publish-central` job summary prints it).

### What it does

1. Copies `ghcr.io/cognisn/spark:<version>` to the ECR repository by version tag, without a
   rebuild, with `scripts/copy-image.sh <version>` (see [The image copy](#the-image-copy)).
   The same digest already in ECR is success; a different digest fails.
2. `scripts/deploy-test.sh <version>` first verifies the image, before any other call and on
   every path, a re-run included: the test copy's digest must be the digest `publish-central`
   signed, and the central tag of the version must name that digest with a valid signature by
   the release key (`scripts/verify-image.sh`, see
   [Verifying an image](release-images.md#verifying-an-image)). If it does not verify, the job
   fails and nothing is changed, so the test stack only ever runs a signed digest.
3. It reads the service stack, which must be in
   `CREATE_COMPLETE`, `UPDATE_COMPLETE` or `UPDATE_ROLLBACK_COMPLETE`.
4. **The hardening check**, on every path and before any change (see
   [The hardening check](#the-hardening-check)). If `RunningImageTag` already equals the
   version (a re-run), it then goes straight to the checks.
5. **Change set one** sets `MigrateImageTag`, so the one-off migrate task definition points at
   the new image. Only `MigrateTaskDefinition` may change.
6. **The migrate task** runs from the stack's outputs (cluster, task definition, subnets and
   security group, Fargate, no public IP, ECS Exec off) and must stop with exit code 0 from its
   `spark-migrate` container: `spark migrate && spark bootstrap`. If it fails, the job stops
   with the stop reason and exit code, and the services are not touched. The job's role cannot
   read the task's log; read the migrate log group.
7. **Change set two** sets `ImageTag`. Only the web and worker task definitions and the two
   services may change. The job waits up to 30 minutes for the stack to finish updating and
   fails, printing the failed stack events, on anything but `UPDATE_COMPLETE`.
8. The checks: `RunningImageTag` equals the version; each service's primary deployment uses the
   stack's task definition, rollout `COMPLETED`, running count equal to desired count and at
   least 1; and `GET /ready` answers 200 with the version, retried for a bounded time. The job
   summary reports these checks and no identifier.

Both change sets reuse the stack's current template (`--use-previous-template`), are named
`spark-deploy-...`, run as the CloudFormation execution role and list only the resource types
`AWS::ECS::TaskDefinition` and `AWS::ECS::Service`; every other parameter is
`UsePreviousValue`. Each change is checked before execution, and a change set with anything
else, or any removal, is deleted and the deploy fails. The job never registers a task
definition, updates a service or updates the stack without a change set.

### The hardening check

Before it changes anything, `scripts/deploy-test.sh` reads the stack's three task definitions
(migrate from the stack's outputs, web and worker from its resources) with
`aws ecs describe-task-definition`, and both services with `aws ecs describe-services`. Both
change sets reuse the stack's current template, so the task definitions they register carry
the same settings as the ones it reads. Every container that runs the image from
`ECR_REPOSITORY_URI` must have each control of
[Container hardening](container-hardening.md#ecs-on-fargate):

- `user` `10001:10001` (or `10001`);
- `readonlyRootFilesystem` true, and `privileged` not true;
- `linuxParameters.capabilities.drop` including `ALL`, and nothing in `capabilities.add`;
- `linuxParameters.initProcessEnabled` true;
- a mount at `/tmp`, not read-only, of a volume the task definition declares with no host
  path and no EFS, Docker or FSx configuration (a task volume on the task's ephemeral
  storage).

Each service must have `enableExecuteCommand` false, and the script starts the migrate task
with `--no-enable-execute-command`. Other containers (a log router, for example) are not
checked. A task definition or service the job cannot read counts as a failure.

Every missing control is listed, by task and container name, in the log and in the job
summary under **Hardening check**, with no identifier. What happens next is set by
`HARDENING_CHECK`, whose default is `HARDENING_CHECK_DEFAULT` in the script:

| Value | A missing control |
|---|---|
| `enforce` | fails the job with `Hardening check: FAILED ..., nothing was changed`, before any change set |
| `warn` | is a warning (`Hardening check: NOT MET ...`); the deploy goes on |
| `enforce-from:YYYY-MM-DD` | is a warning before that UTC date, and fails the job from it |

The job's role needs `ecs:DescribeTaskDefinition` (which takes `Resource: "*"` only) besides
the `ecs:DescribeServices` it already has.

### The image copy

The job installs `crane` (go-containerregistry) from a pinned release, checked against its
published sha256 before it is unpacked (`.github/actions/install-crane`, which the central copy
uses too), and `scripts/copy-image.sh` uses it to copy the image.
`crane copy` moves the manifest and every blob exactly as they are, so the digest in ECR equals
the digest in ghcr.io. The digest must match because it is the image's identity: the digest
that was built, checked and published is the one that runs in the test environment, and a
different digest would mean the tested image is not the deployed one. (`docker buildx imagetools
create` is not used: it writes a new manifest, so the digest changes.) The script:

1. reads the digest of the published image;
2. if ECR already holds the tag at that digest, succeeds and copies nothing (a re-run);
3. if ECR holds the tag at a different digest, fails;
4. otherwise copies the image and requires the digest ECR then reports to equal the published
   digest.

The ECR repository has immutable tags, so a tag that exists at another digest cannot be
overwritten, and re-running the job does not help. Publish a new release candidate (the next
`rcN`) instead; the old tag stays in ECR until its lifecycle policy removes it. The messages
the script prints carry no AWS identifier.

### What it never changes

The stack template, the sizes, the web count, `MigrateShellCommand` and the other parameters,
any other stack, the network, the data stores and the secrets. Changes to the service stack's
template are made by hand and approved by Matthew; the job cannot make them.

### The first deploy

The stack is created with `ImageTag` and `MigrateImageTag` set to `none`, so nothing runs. The
first release-candidate tag runs the migrate task (which brings both schemas to head and runs
the headless bootstrap) and then starts the web service and the worker, with no manual step.

### A failed deploy

A refused AWS call fails the job with the AWS message shown (ARNs hidden), so the deploy role's
policy can be adjusted. If a service cannot reach a healthy state, ECS's deployment circuit
breaker rolls the service back, the stack update ends in `UPDATE_ROLLBACK_COMPLETE`, and the job
fails with the failed events; the stack then runs the previous version. The job never
continues or repairs a rollback. A stack in any other state (for example
`UPDATE_ROLLBACK_FAILED`) is refused at the start, and is repaired by hand.

### A migrate task that fails after the logins exist

If the migrate task fails after `spark bootstrap` has created the `spark_web` and
`spark_worker` logins (for example a lost secrets write), nothing has started, because the
deploy stops before either service changes. Image tags in ECR are immutable, so recover by
publishing the next release candidate: its deploy runs the migrate task again. `spark
bootstrap` is safe to re-run. It leaves the logins and a usable signing key as they are,
generates only the identity keys that are missing, replaces a signing key whose private half
the store does not hold, and then invites the first administrator if none exists.

Start-up confirms the three identity keys from a fresh read of the store before it builds the
identity, and stops with a message such as `the secrets store did not keep warden.totp-key`
if one is absent. To check by hand which keys exist, list the bundle's key names without
reading any value, for example `aws secretsmanager get-secret-value --secret-id <bundle>
--query SecretString --output text | jq 'keys'`, and look for `warden.totp-key`,
`spark.csrf-key` and one `warden.signing-key.<id>` (the bundle's names carry your
configured prefix).

### Going back to an earlier version

Set the stack's `ImageTag` (and `MigrateImageTag`) to an earlier version tag still held in ECR,
which keeps the newest 10 images, through a change set in the console or the CLI. Migrations
are not rolled back: the earlier image runs against the database as it now is, so check that
the earlier version accepts the current schema first (Spark's web start refuses a database that
is not at its head).

## The central repository and the release role

Release images for client instances come from the central repository in the shared services
account, not from ghcr.io or the test account. The `publish-central` job pushes and signs them
there; [Release images](release-images.md) describes the whole path. This section is the AWS
contract the job needs. The account IDs, key ID and names below are placeholders: `123456789012` is the
shared services account, and `CHANGEME` stands for the key ID and for GitHub's numeric owner and
repository IDs.

### The spark-release environment

A GitHub environment `spark-release`, with deployments limited to tags matching `v*`, holds four
variables (the job masks each before a step can print it):

| Variable | Holds |
|---|---|
| `AWS_REGION` | The central repository's Region, for example `ap-southeast-2` |
| `AWS_RELEASE_ROLE_ARN` | The release role, for example `arn:aws:iam::123456789012:role/spark-release` |
| `CENTRAL_ECR_REPOSITORY_URI` | `123456789012.dkr.ecr.ap-southeast-2.amazonaws.com/spark` |
| `IMAGE_SIGNING_KEY` | cosign's key reference, `awskms:///arn:aws:kms:ap-southeast-2:123456789012:key/CHANGEME` |

### The release role's trust

The role trusts GitHub's OIDC provider in the shared services account, for this repository in
the `spark-release` environment only, in both the plain subject form and the immutable form
that carries GitHub's numeric owner and repository IDs (as the test deploy role does):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GitHubReleaseEnvironmentOnly",
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": [
            "repo:Cognisn/spark:environment:spark-release",
            "repo:Cognisn@CHANGEME/spark@CHANGEME:environment:spark-release"
          ]
        }
      }
    }
  ]
}
```

### The release role's permissions

Push to the one repository, sign with the one key, and nothing else: no delete, lifecycle,
repository policy or key administration actions.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EcrSignIn",
      "Effect": "Allow",
      "Action": "ecr:GetAuthorizationToken",
      "Resource": "*"
    },
    {
      "Sid": "PushToTheCentralSparkRepository",
      "Effect": "Allow",
      "Action": [
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:InitiateLayerUpload",
        "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload",
        "ecr:PutImage"
      ],
      "Resource": "arn:aws:ecr:ap-southeast-2:123456789012:repository/spark"
    },
    {
      "Sid": "SignWithTheReleaseKey",
      "Effect": "Allow",
      "Action": ["kms:Sign", "kms:GetPublicKey", "kms:DescribeKey"],
      "Resource": "arn:aws:kms:ap-southeast-2:123456789012:key/CHANGEME"
    }
  ]
}
```

The read actions (`BatchGetImage`, `GetDownloadUrlForLayer`) let the job read the tag back and
verify the signature. `kms:DescribeKey` is needed as well as `kms:Sign` and `kms:GetPublicKey`:
cosign v2.6.5's AWS KMS provider reads the key's signing algorithm with `DescribeKey` before it
signs, verifies or prints the public key. The key is an asymmetric `SIGN_VERIFY` key; its key
policy grants the release role the same three actions and no others.

### The test deploy role reads the central repository

`scripts/deploy-test.sh` verifies the signature in the central repository before it changes the
test stack, so the test deploy role (in the test account) needs to read that one repository:

- in the test deploy role's policy, `ecr:BatchGetImage` and `ecr:GetDownloadUrlForLayer` on
  `arn:aws:ecr:ap-southeast-2:123456789012:repository/spark` (it already has
  `ecr:GetAuthorizationToken`);
- in the central repository's policy, the same two actions for the test account (already the
  case if the policy allows pulls by every workload account of the organisation).

The test deploy does not use the KMS key: it verifies with the public key in the `spark-test`
variable `IMAGE_SIGNING_PUBLIC_KEY`.

### Lifecycle rules

Signatures are kept as tags named `sha256-<digest>.sig` beside each version tag. A rule that
expires untagged images (for example after 7 days) leaves them alone. A rule that counts or
ages tagged images would count those tags too and could remove a release's signature, after
which `verify-image.sh` refuses the release: exclude the `sha256-` tag prefix from any such
rule.

### Before changing a client instance's image

Run `scripts/verify-image.sh` against the central reference before changing a Spark instance's
image tag, and change it only if it exits 0; see
[Verifying an image](release-images.md#verifying-an-image).
