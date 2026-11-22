# Identity: Administrators, Users, Single Sign-On and Mail

A Spark server signs people in through warden (`cognisn-warden`), which runs inside
the web and worker processes over the same PostgreSQL database. warden keeps the
users, sessions, roles, invitations, API keys and the audit trail; Spark provides the
screens. There is no separate identity service to deploy.

<a id="the-first-administrator"></a>

## Bootstrap

A server comes up with nobody at a keyboard. `spark bootstrap` runs once as the schema
owner after `spark migrate`, before web and worker start: Compose's `migrate` service
runs `spark migrate && spark bootstrap`, and a cloud deployment runs the same command
in a one-off task. Personal mode never bootstraps.

### Settings

Each setting is read from `config.yaml`, a `SPARK__` variable or the AWS settings
document, at the same dotted path. Bootstrap refuses to run unless all of them are
complete and valid, and its refusal lists every problem by both names, never a value.

| Setting | `SPARK__` variable | Required |
|---|---|---|
| `bootstrap.admin.email` | `SPARK__BOOTSTRAP__ADMIN__EMAIL` | Yes: the first administrator's address |
| `bootstrap.admin.first_name` | `SPARK__BOOTSTRAP__ADMIN__FIRST_NAME` | Yes |
| `bootstrap.admin.last_name` | `SPARK__BOOTSTRAP__ADMIN__LAST_NAME` | Yes |
| `bootstrap.database.web_url` | `SPARK__BOOTSTRAP__DATABASE__WEB_URL` | With [restricted roles](database.md#restricted-roles), with the next |
| `bootstrap.database.worker_url` | `SPARK__BOOTSTRAP__DATABASE__WORKER_URL` | With restricted roles, with the previous |
| `worker.audit_purge_database_url` | `SPARK__WORKER__AUDIT_PURGE_DATABASE_URL` | With restricted roles and an audit retention |
| `mail.smtp.host`, `port`, `tls`, `sender_address`, `sender_name`, `username` | `SPARK__MAIL__SMTP__HOST` and so on | A host and a sender address, unless already stored ([Mail](#mail)) |
| `bootstrap.bedrock.region` | `SPARK__BOOTSTRAP__BEDROCK__REGION` | No: an AWS Region such as `ap-southeast-2`; set, bootstrap adds an Amazon Bedrock connection that uses the task role ([AWS](aws.md#bedrock-through-the-task-role)) |
| `bootstrap.bedrock.name` | `SPARK__BOOTSTRAP__BEDROCK__NAME` | No: its display name (Amazon Bedrock (task role)) |
| `bootstrap.bedrock.residency_regions` | `SPARK__BOOTSTRAP__BEDROCK__RESIDENCY_REGIONS` | No: comma-separated Regions it may process in, including its own, for the cross-Region inference profiles |
| `bootstrap.wait_seconds` | `SPARK__BOOTSTRAP__WAIT_SECONDS` | No: how long web and worker wait for bootstrap (600) |
| `bootstrap.wait_interval_seconds` | `SPARK__BOOTSTRAP__WAIT_INTERVAL_SECONDS` | No: how often they check (5) |

The database URLs may be `secret://<name>` references to the secrets store. A URL with
`sslmode=verify-full` (or `verify-ca`) needs a readable root certificate, named by
`sslrootcert` in the URL or by `PGSSLROOTCERT`, and the owner's connection must then be
encrypted; the refusal names the setting, never the URL.

### What it does

In one PostgreSQL advisory lock, so two copies started together agree:

1. checks the settings and the owner's connection;
2. creates or updates the restricted database logins and their grants, when they are
   configured ([Database roles](database.md#restricted-roles));
3. creates the instance identity in the secrets store: warden's signing key, the TOTP
   key and Spark's CSRF key ([Secrets](secrets.md#what-the-store-holds));
4. stores the start-up `mail.smtp` values as the instance's mail settings, while the
   database holds none (audited as `spark.settings.instance.changed` by
   `spark-bootstrap`);
5. invites the first administrator through warden, as "First Last", with the
   `administrator` role granted when they accept. warden sends the invitation by mail
   inside its own transaction, so if delivery fails nothing is written and bootstrap
   exits non-zero; run it again once mail works. If anyone already holds or has been
   invited to `administrator`, nothing is sent;
6. records that bootstrap is complete (the `spark.bootstrap` table).

It prints one line per step, never an address, a URL, a password or a link. A later run
finds the record and sends nothing, but repeats steps 2 to 4, so a changed login
password, or a table a new version adds, is applied. Exit code 0 means done; 2 means a
refusal, with every problem on standard error.

The invitation expires after seven days. To send it again (it must not yet be
accepted):

```bash
docker compose -p spark -f deploy/compose.yaml run --rm migrate "spark bootstrap --resend-invitation"
```

### Web and worker wait for it

Web and worker may start before bootstrap has finished, for example when a deployment
starts them together. The worker always waits for the instance identity (it may only be
allowed to read the secrets store), and both wait for the bootstrap record when
`bootstrap.admin.email` is set. Each logs one line, `spark worker is waiting for spark
bootstrap (missing: ...)`, checks every `bootstrap.wait_interval_seconds`, and starts as
soon as everything is there. After `bootstrap.wait_seconds` it exits, naming what is
missing. While web waits, its port answers `GET /health` with 200 and every other request, `/ready` included, with 503, so a health check keeps passing; once bootstrap is complete, Spark takes the port.

An installation without `bootstrap.admin.email` (one that does not run bootstrap) keeps
the old behaviour: the first web start creates any missing identity key.

## Restoring an administrator

`spark identity create-admin` creates or restores an administrator with a password on
standard input. A new server does not need it, since bootstrap invites the first
administrator; it is for recovery, when no administrator can sign in:

```bash
docker compose -p spark -f deploy/compose.yaml run --rm -T web \
    identity create-admin --email admin@example.com < password.txt
```

It prints `administrator <id>, created, administrator role assigned`. Without `-T`
and the redirect it asks for the password twice instead. The password must be at
least 12 characters (`warden.identity.password_min_length`). Delete `password.txt`
afterwards.

For an existing email address it keeps the password, gives the administrator role if
it is missing, and prints `already existed`.

`--link-user-guid GUID` binds an existing Spark user guid to the administrator, so
data made under it stays theirs. It is only for a server that ran a 0.3.0 pre-release
before identity existed, whose data belongs to one interim guid; a new server does
not need it, and personal users are moved [with a bundle](from-personal.md).

## Signing in the first time

Follow the invitation's link, set a password, and sign in at
`https://spark.example.com/auth/sign-in`. Two-step verification is on by default, so
the first sign-in asks the administrator to add Spark to an authenticator app (a QR
code, or the key typed in) and shows ten recovery codes once. Keep the recovery codes
somewhere safe.

## Roles

| Role | Holds |
|---|---|
| `administrator` | Everything: instance settings and secrets, model connections with their metering and assignments (`spark.admin.connections`), users, roles, invitations and API keys, the audit trail, usage, bundle import, and every user capability |
| `auditor` | The audit trail at `/admin/audit` and its CSV and NDJSON export, and nothing else |
| `power_user` | A user's capabilities, plus scheduled actions, sub-agents and the instance-wide knowledge graph |
| `user` | Chat, debates and panels, memories, skills, their own knowledge graphs and files, the built-in tools a server offers, and their own API keys |

A user may hold several roles; their permissions add up. warden never lets the last
active administrator lose the role or be suspended.

On a server the system command, filesystem and email tools are not available to
anyone: they act on the host Spark runs on. MCP servers are granted to roles on
**Administration > Tools and MCP servers** (each server lists its roles), and a user sees only the servers
their roles are granted. A scheduled action runs with its owner's roles as they are
when it runs.

## Inviting users

An administrator invites from **Administration > Invitations** (`/admin/invitations`),
choosing the roles granted when the invitation is accepted. An invitation works once
and expires after seven days (`warden.identity.invitation_ttl_seconds`). Users,
their roles, suspension, password resets and second factor resets are managed from
**Administration > Users**.

With [mail](#mail) set up, invitations and resets are sent by email. Without it the
link is shown to the administrator once, to pass on privately.

## Two-step verification

`SPARK__WARDEN__IDENTITY__REQUIRE_MFA` (default `true`) makes every password sign-in
use an authenticator app. Leave it on. A user who has lost both their authenticator
and their recovery codes needs an administrator to reset their second factor from
their user page; confirm who they are some other way first. They set it up again at
their next sign-in. warden 0.1.0 cannot replace a confirmed authenticator, so
replacing a phone is also an administrator's reset.

Other warden settings (`SPARK__WARDEN__IDENTITY__...`) include
`access_token_ttl_seconds` (900), `session_lifespan_seconds` (43200, twelve hours),
`password_min_length` (12), `sign_in_attempt_limit` (10 per
`sign_in_window_seconds`, 900) and `totp_issuer`, the name shown in the authenticator
app.

## Passwords and forced changes

warden 0.1.0 enforces its default password policy wherever a password is set (the
first administrator, an invitation, a reset, the account page): at least
`password_min_length` characters, and not one of about 100,000 very common passwords.
A refused password shows warden's own messages, for example "Use at least 12
characters. This password is too common."

When warden requires a new password before it issues a session (an administrator asked
for it, the password expired, it no longer meets the policy, or it is on a watch list),
the sign-in goes on, after the password and any second factor, to a new-password step
on the challenge screen. No session is set until warden accepts the new password. After
a forced enrolment in two-step verification the recovery codes are shown first, then
the new-password step. A user whose current password is too common is asked for a new
one at their next password sign-in.

## API keys

Users create their own API keys at **Account > My API keys** (`/account/api-keys`), with
no more than their own permissions. A key is sent as `Authorization: Bearer wdn_...`
and is never combined with a session cookie. Keys expire after at most 365 days
(`warden.identity.api_key_max_lifetime_days`). Requests made with an API key are
granted no MCP server.

## Single sign-on

warden signs users in with one OpenID Connect provider (Microsoft Entra ID, Google,
Okta, Keycloak and others). Single sign-on does not create accounts: a user must
already exist, created or invited, before they can sign in through it.

An administrator sets it up on the **Single sign-on** page (`/admin/sso`, from the
**Single sign-on settings** button on the Users page):

1. Register Spark with the provider as a web application:
   - the redirect URI the page shows (with a copy button), for example
     `https://spark.example.com/identity/auth/sso/callback`;
   - scopes `openid`, `email` and `profile`;
   - a client secret.
2. On the page, enter the issuer URL (exactly as the provider's discovery document
   states it, trailing slash included; it must use https), the client ID and the client
   secret, and **Save**. The secret is written to the secrets store under
   `warden.sso.client-secret` and is never shown again; leave the field blank to keep
   it, or enter a new one to replace it.
3. **Test** checks the saved settings: the client secret, the discovery document at
   `<issuer>/.well-known/openid-configuration` (and that it names the same issuer), the
   signing algorithms and the provider's signing keys. It signs nobody in.
4. Tick **Offer single sign-on on the sign-in and invitation pages** and **Save**. It
   cannot be switched on without a stored client secret.

Until single sign-on is on and fully configured (issuer URL, client ID and a stored
client secret), Spark offers it nowhere: the sign-in and invitation pages have no single
sign-on button, My account shows the sign-in method as Password, and the Users and
Invitations pages offer only a password. A user who signs in only through single sign-on
is flagged "Cannot sign in: single sign-on is off" on the Users page; change their
sign-in method on their page and send them a password reset link. Switching single
sign-on off ends every session it issued, and while any active, suspended or invited user
signs in only through it the page asks for confirmation first and names them.

Every change is audited by warden with field and secret names only, never values
(`warden.identity.sso.settings_changed`, `warden.identity.sso.client_secret_set`,
`warden.identity.sso.configuration_tested`, `warden.identity.sso.disabled`).

The page's values are run-time overrides over the start-up settings, which still work
and are the defaults until an administrator saves the page:

```
SPARK__WARDEN__SSO__ENABLED=true
SPARK__WARDEN__SSO__ISSUER_URL=https://login.example.com/realms/example
SPARK__WARDEN__SSO__CLIENT_ID=spark
```

with the client secret placed in the secrets store under `warden.sso.client-secret`
([placing a secret](secrets.md#placing-a-secret-in-the-store)). Optionally
`SPARK__WARDEN__SSO__ALLOWED_EMAIL_DOMAINS=example.com` accepts only addresses in your
own domains, and `SPARK__WARDEN__SSO__PROVIDER_PRESET` (`entra`, `google`, `okta` or
`generic`) is a label that changes nothing; neither is on the page.

warden accepts only asymmetrically signed ID tokens (RS256, ES256, PS256), and checks
the state, nonce and PKCE of every sign-in. A provider behind a private certificate
authority needs `SSL_CERT_FILE` naming a bundle that holds that authority and the public
ones, mounted into the containers.

> Not run for this guide: a sign-in against a real provider. Spark's tests run single
> sign-on and the page's Test against synthetic providers, and a live test against a
> local OpenID provider is run by hand before a release.

## Mail

warden sends invitations, resends, password reset links, signed-out reset requests and
second factor notices by email once an SMTP server is set. Mail is set on
**Administration > Mail** (`/admin/mail`). On a new server,
bootstrap stores the `SPARK__MAIL__SMTP__...` start-up values there first, because the
first administrator's invitation is sent by mail; after that, the page's values are the
ones used and the start-up values are ignored:

| Setting | Default | Meaning |
|---|---|---|
| `mail.smtp.host` | empty | The SMTP server. Empty means no mail (below) |
| `mail.smtp.port` | 587 | 587 with STARTTLS, or 465 with implicit TLS |
| `mail.smtp.tls` | `starttls` | `starttls`, `implicit`, or `none`, which is accepted only for a relay on a loopback address (`localhost`, `127.0.0.1`, `::1`) |
| `mail.smtp.sender_address` | empty | The From address; required once a host is set |
| `mail.smtp.sender_name` | empty | The From name |
| `mail.smtp.username` | empty | Leave empty for a relay that needs no sign-in |
| `mail.smtp.password` | | A secret, kept in the secrets store as `mail_smtp_password`: type it on the page, or [place it](secrets.md#placing-a-secret-in-the-store) before the first start |

The server's certificate must verify against the system trust store, with TLS 1.2 or
later, and each message must be delivered within 30 seconds. The page refuses plain
text to any host that is not loopback, and a host without a sender address. Changes
apply to the next message on every replica, without a restart.

Press **Send test message** to send one to your own address. If it fails, the page
says why (for example, the server refused the username or password) without echoing
the credential or the server's reply. Each test is audited as `spark.mail.test_sent`.

### Without mail

While `mail.smtp.host` is empty, nothing is sent and warden hands each link to
Spark's link capture instead:

- an administrator who invites a user, resends an invitation or resets a password
  sees the link once, on screen, to pass on privately;
- a signed-out user asking for a reset at `/auth/password-reset` gets the usual
  neutral answer and no link, so they ask an administrator instead.

A server can run like this, but users cannot reset their own passwords.

> Not run for this guide: delivery to a real mail server. Spark's tests deliver to a
> local SMTP server over STARTTLS, implicit TLS and plain text, and the release's
> live suite checks invitation mail through a mail catcher.

## Connections, credits and usage

A user sees and uses only the model connections assigned to them or to one of their
roles (a connection assigned to no one is seen by administrators only). Administrators
assign connections, and on a metered connection set each assignment's monthly credit
allowance, on **Administration > Connections and models** (`/admin/connections`). Users are warned from
80%, and new requests on that connection are refused at 100% until the allowance resets
at 00:00 UTC on the first of the month. [Connections](../connections.md) and
[Metering](../metering.md) describe the rules in full. Usage by month, user,
connection and model, with tokens and credits, is at **Administration > Usage and budgets**
(`/admin/usage`), with a CSV export.

## The audit trail

warden keeps an append-only, hash-chained audit trail in `warden.audit_events`:
sign-ins and administration, and Spark's own events (settings and secret changes by
name, tool refusals, MCP calls, bundle imports, mail tests). Auditors and
administrators read it at `/admin/audit` and export it. Details never carry secret values or
message content. Retention and its database grants are on the
[Database](database.md#wardens-audit-trail-and-its-purge) page.
