# Secrets

A Spark server keeps every secret in konfig's secrets store and nowhere else: never
in the database, never in `config.yaml`, never in a bundle and never in the browser.
A secret an administrator types on the instance settings page is written to the
store, shown afterwards only as "Set", and cannot be read back.

Server mode accepts two stores, both shared by every web and worker container:

| Store | When | Chosen by |
|---|---|---|
| konfig's **encrypted file**, keyed by `KONFIG_MASTER_KEY` | Off AWS, and the Compose default | `SPARK__SECRETS__BACKEND=encrypted_file` with `SPARK__SECRETS__FILE_PATH` |
| **AWS Secrets Manager** | When Spark runs in AWS | `KONFIG_AWS_SECRETS_MANAGER` (one secret holding a JSON bundle), or `SPARK__SECRETS__BACKEND=aws_secrets_manager` (one secret per name) |

Server mode refuses to start with any other store, including the OS keychain a
personal installation uses, because every replica must read the same secrets. It also
refuses the encrypted file without `KONFIG_MASTER_KEY`.

## What the store holds

| Name | What | Written by |
|---|---|---|
| `warden.signing-key.<id>` | warden's Ed25519 signing keys (session tokens, audit checkpoints) | the first web start |
| `warden.totp-key` | The key that encrypts users' authenticator secrets in the database | the first web start |
| `spark.csrf-key` | The key that binds CSRF tokens to warden sessions | the first web start |
| `providers_anthropic_api_key`, `providers_google_gemini_api_key`, `providers_xai_api_key`, `providers_aws_bedrock_access_key`, `providers_aws_bedrock_secret_key`, `providers_aws_bedrock_session_token` | Provider credentials | the instance settings page |
| `embedded_tools_web_brave_api_key`, `embedded_tools_web_google_api_key`, `embedded_tools_web_bing_api_key` | Web search keys | the instance settings page |
| `voice_elevenlabs_api_key` | ElevenLabs | the instance settings page |
| `mail_smtp_password` | The SMTP password for identity mail | the instance settings page, or [placed](#placing-a-secret-in-the-store) |
| `mcp_server_<name>_<field>` | An MCP server's bearer token, password or header values | the instance page's MCP editor |
| `warden.sso.client-secret` | The single sign-on client secret | [placed](#placing-a-secret-in-the-store) |
| `database_url` | The database URL, when `database.url` is `secret://database_url` | [placed](#placing-a-secret-in-the-store) |

A setting's secret name is its dotted key with `.` replaced by `_`.

## Off AWS: the encrypted file

The Compose file sets:

```yaml
SPARK__SECRETS__BACKEND: encrypted_file
SPARK__SECRETS__FILE_PATH: /var/lib/spark/data/spark/secrets.enc
```

The file is on the shared `spark-data` volume, readable only by the `spark` user
(mode 0600). konfig encrypts it with a key derived from `KONFIG_MASTER_KEY`, and
takes an exclusive lock on the sidecar `secrets.enc.lock` for every write, so
replicas saving at the same time keep each other's changes. If the volume is on
network storage it must honour POSIX record locks; NFS, including Amazon EFS, does.

### The master key

`KONFIG_MASTER_KEY` unlocks every secret on the server. Generate it once:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

- **Give every web, worker and migrate container the same key.** The Compose file
  reads it from the env file for all three.
- **Keep it out of the image, the repository and the database.** In Compose it lives
  only in `deploy/spark.env` (mode 0600). A secrets manager of your own, or your
  orchestrator's secret mechanism, is better still.
- **Store a copy apart from your backups.** A backup of `secrets.enc` without the key
  is unreadable, and the two together are every credential the server has. Keep the
  key in a password manager or vault that the backup system cannot reach.
- **Losing it is not recoverable.** Without the key, the signing keys, the TOTP key
  and every provider key are gone: existing sessions end, every user must set up
  two-step verification again (an administrator resets each one), and every provider
  key must be entered again.
- **Spark 0.3.0 cannot change the key.** A container given a different key for an
  existing file stops with "The master key does not match the secrets file ... Every
  process sharing this file must use the same KONFIG_MASTER_KEY" and leaves the file
  as it was. Treat the key as fixed for the life of the file.

## On AWS: Secrets Manager

When Spark runs in AWS (ECS, EC2), keep the secrets in AWS Secrets Manager and give
the containers an IAM role instead of a master key. Two layouts are available; the
bundle needs one secret and the narrowest policy, so prefer it.

> These AWS steps were not run for this guide (no AWS account was used). The settings
> and IAM actions come from konfig's AWS backends; Spark's tests prove the bundle's
> refresh on PostgreSQL notifications against a stubbed Secrets Manager.

### One bundle secret (recommended)

1. Create one secret holding a JSON object, for example `spark/production`, with
   the value `{}`. Spark adds names to it as administrators save secrets and as the
   first web start creates warden's keys.
2. Give the containers `KONFIG_AWS_SECRETS_MANAGER` set to that secret's ARN, for
   example
   `KONFIG_AWS_SECRETS_MANAGER=arn:aws:secretsmanager:ap-southeast-2:111122223333:secret:spark/production-AbCdEf`.
   It overrides `SPARK__SECRETS__BACKEND`, so the Compose file's `encrypted_file`
   setting can stay; `KONFIG_MASTER_KEY` is then not needed.
3. Grant the task or instance role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SparkSecretsBundle",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue"
      ],
      "Resource": "arn:aws:secretsmanager:ap-southeast-2:111122223333:secret:spark/production-*"
    }
  ]
}
```

If the secret is encrypted with a customer managed KMS key, also allow `kms:Decrypt`
and `kms:GenerateDataKey` on that key.

Reads are cached for `secrets.aws.cache_ttl` seconds (default 300,
`SPARK__SECRETS__AWS__CACHE_TTL`); a secret saved on one replica refreshes the cache on
every replica at once (below). On first start with an empty bundle, konfig writes a
placeholder for each `secret://` reference in the settings, as a template to fill in;
set `KONFIG_AWS_SEED=false` to stop it writing anything.

### One secret per name

`SPARK__SECRETS__BACKEND=aws_secrets_manager`, with `SPARK__SECRETS__AWS__REGION` and
optionally `SPARK__SECRETS__AWS__PREFIX` (for example `spark`, giving secrets named
`spark/warden.totp-key`). konfig creates a secret for each name when it is first
saved, and deletes it without a recovery window when it is cleared. The role needs:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SparkSecretsPerName",
      "Effect": "Allow",
      "Action": [
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:CreateSecret",
        "secretsmanager:DeleteSecret"
      ],
      "Resource": "arn:aws:secretsmanager:ap-southeast-2:111122223333:secret:spark/*"
    },
    {
      "Sid": "SparkSecretsList",
      "Effect": "Allow",
      "Action": "secretsmanager:ListSecrets",
      "Resource": "*"
    }
  ]
}
```

### Credentials

konfig uses boto3's default credential chain. Use the ECS task role or the EC2
instance profile; do not put long-lived access keys in the env file. konfig can
also read non-secret start-up settings from a Secrets Manager secret
(`KONFIG_AWS_SETTINGS`, read only), which needs `GetSecretValue` on that secret; the
env file works as well and is what this guide uses.

## How a saved secret reaches every replica

1. An administrator saves (or clears) a secret on one web replica. That replica writes
   it to the store and sends a PostgreSQL notification on the
   `spark_instance_settings` channel. The notification carries the secret's **name**
   only, never its value.
2. Every web replica listens on that channel. On a notification naming a secret, it
   has konfig re-read the store (the shared file, or the Secrets Manager bundle past
   its cache), then reloads its settings.
3. The reload rebuilds what uses the secret: the providers, voice and web search
   keys. The change applies within a few seconds, without a restart.

A replica also re-reads the store whenever its notification listener reconnects, so
one that missed a notification while the database was unreachable catches up. The
worker builds the model clients for each knowledge graph job from the store as it is
then.

This needs cognisn-konfig 0.5.0 or later, which Spark 0.3.0 requires. The image
already has it.

## Placing a secret in the store

Secrets that have no field on the instance settings page (the single sign-on client
secret, a `secret://` database URL), or that you want in place before the first
start (the SMTP password), are written with konfig from a one-off container. Put the
value alone on one line in a file only you can read, then:

```bash
docker compose -p spark -f deploy/compose.yaml run --rm --no-deps -T --entrypoint python web \
    -c "import sys; from konfig import AppContext; ctx = AppContext(name='Spark', env_prefix='SPARK').__enter__(); ctx.secrets.set(sys.argv[1], sys.stdin.readline().rstrip('\r\n')); print('stored', sys.argv[1])" \
    warden.sso.client-secret < client-secret.txt
```

It prints `stored warden.sso.client-secret` and never the value. Replace the last
argument with the secret's name (from the table above) and the file. The container
uses the same store as the stack, the encrypted file or Secrets Manager, from the
same env file. Delete the file afterwards.

A secret placed this way does not send a notification. Place it before the first
start, or restart the web and worker containers afterwards:

```bash
docker compose -p spark -f deploy/compose.yaml restart web worker
```
