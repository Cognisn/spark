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
| `bootstrap.bedrock.models` | `SPARK__BOOTSTRAP__BEDROCK__MODELS` | No: comma-separated model ids, for example `amazon.nova-lite-v1:0,au.anthropic.claude-sonnet-4-6` (a JSON list in a settings document also works). Set, the connection bootstrap creates offers exactly those models at the Standard tier and every other model is off, until an administrator changes it. Not applied to a connection that exists |
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
`https://spark.example.com/auth/sign-in`. Two-step verification is always on, so the
first sign-in asks the administrator to add Spark to an authenticator app (a QR code,
or the key typed in) and shows ten recovery codes once. Keep the recovery codes
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

Every password sign-in asks for a second factor: a code from an authenticator app, or
a recovery code. It cannot be switched off. A user who has no authenticator app yet is
asked to set one up straight after their password, and is shown their ten recovery
codes once. Single sign-on asks for nothing more: the identity provider does its own
checks.

`warden.identity.require_mfa` is deprecated. `true` does nothing, and `false` stops
Spark from starting (see [Upgrading to warden 0.2.0](#upgrading-to-warden-020)).

A user who has lost both their authenticator and their recovery codes needs an
administrator to reset their second factor from their user page; confirm who they are
some other way first. They set it up again at their next sign-in. Replacing a phone is
also an administrator's reset.

Other warden settings (`SPARK__WARDEN__IDENTITY__...`) include
`access_token_ttl_seconds` (900), `session_lifespan_seconds` (43200, twelve hours),
`password_min_length` (12), `sign_in_attempt_limit` (10 per
`sign_in_window_seconds`, 900) and `totp_issuer`, the name shown in the authenticator
app.

## Passkeys and security keys

Users register passkeys (a fingerprint, face or PIN on their device, or a synced
passkey) and security keys from **My account > Passkeys and second factors**
(`/account/mfa`), give each a name, and rename or remove them there. Every password
sign-in then offers the key beside the authenticator app, and a user who has no
second factor yet may register one instead of setting up an app. warden refuses to
remove a user's last second factor. A user's first second factor comes with ten
recovery codes, shown once.

**Signing in with a passkey alone.** The sign-in page offers **Sign in with a
passkey**, and where the browser supports it, the browser's autofill on the email
field lists the user's passkeys. There is no password and no second step. warden
decides who may: users whose sign-in method is a passkey only, always, and other
users only while passkeys are an allowed second factor. Every refusal shows the same
message; the audit trail records why.

**Passkey-only users.** An administrator invites someone who will sign in with a
passkey only by choosing **A passkey only** on the invitation form; the invitation
link opens a page that registers a passkey on their device and shows their recovery
codes. For a user who has lost their passkeys, the administrator resets every second
factor from the user's page (**Passkeys and security keys > Reset every second
factor**), confirming who they are some other way first, then sends **a passkey
registration link**. The link works once, for `warden.identity.reset_ttl_seconds`,
registers a passkey without signing in, and without [mail](#mail) is shown to the
administrator once.

**The relying party follows `server.public_base_url`.** Passkeys are tied to the
address users reach Spark on:

- With nothing set, the relying party id is the host of `server.public_base_url`
  (`spark.example.com` for `https://spark.example.com`), and a ceremony is accepted
  only from that address's origin.
- `warden.webauthn.rp_id` may name a parent domain instead (`example.com`), so a
  passkey also works on other hosts under it. It must be the host or a registrable
  suffix of it.
- `warden.webauthn.allowed_origins`, when set, must include `server.public_base_url`.
- Either mismatch stops Spark at start-up with a message naming the settings.
- On an address whose host is an IP address there is no relying party, so passkeys
  are not offered anywhere. Use a host name (`localhost` works for a trial).

Changing the relying party id later strands every registered passkey: users need new
ones. Other settings under `warden.webauthn`: `rp_name` (the name the device shows),
`max_credentials_per_user` (10), `challenge_ttl_seconds` (300) and
`attestation_roots`, trusted roots as PEM certificates (usually a `secret://`
reference) when only certain makes of security key may register; synced passkeys
carry no attestation and are then refused.

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

## How people sign in

**Administration > Users and access > Sign-in** (`/admin/sign-in`, needs the user
administration permission) sets how everyone signs in. There are four methods:

| Method | What it means |
| --- | --- |
| Password + MFA | A password and a second factor. Single sign-on is not offered. |
| Passkey | A passkey alone, with no password. Needs passkeys to work on this server. |
| SSO | Single sign-on through your identity provider, with no password and no passkey. Needs single sign-on set up and switched on. |
| Password + MFA or SSO | Either a password and a second factor, or single sign-on once it is set up. This is the starting default. |

Until single sign-on is set up, the page does not let SSO be chosen, and until
passkeys work (see [Passkeys and security keys](#passkeys-and-security-keys)) it does
not let Passkey be chosen.

### The default and overrides

The method chosen on the Sign-in page is the **default**: every user follows it unless an
administrator gives them a method of their own. On a user's page (**Sign-in method**) the
choices start with **Use the instance default**, which shows what it is now, then the
methods that can be chosen. The Users page shows the method in force for each person,
marks those who follow the default, and shows a change waiting for them. Invitations
offer the same choices, again starting with the default.

### Changes that wait

A change takes effect at once for a user who already holds what the new method needs:
a password for Password + MFA, a passkey for Passkey, a linked single sign-on identity for
SSO. Otherwise it is **pending**: the method in force keeps working, and warden emails a
set-up link where one is needed, a set-password link or a passkey registration link.
Moving to SSO needs no link: it completes the first time the person signs in through
single sign-on. When the change completes, whatever the new method does not allow is
removed (the password hash, and the passkeys and security keys for SSO), and so are the
sessions signed in another way.

A user's page shows the pending change, when it was asked for and when the link was
sent, with **Resend the link** (a new link, and the old one stops working) and **Cancel
the change** (the user stays on the method in force). Account security shows the person
their own method and any change waiting for them. Without a [mail service](#mail) the
link is shown to the administrator once, to pass on privately.

Changing the default moves everyone who follows it in the same way. After saving, the
page says how many people moved at once and how many are waiting, lists those waiting,
and names anyone whose link could not be delivered so each can be resent from their own
page. Without a mail service it lists each person's link once.

### The single sign-on guard

Single sign-on cannot be switched off, or its settings cleared, while the default or an
active user's method is SSO. The Sign-in page lists the people who sign in only through
single sign-on, each linked to their page, so they can be moved first; the
[Single sign-on](#single-sign-on) page lists them again when it refuses.

### Allowed second factors

The Sign-in page also sets which second factors the instance allows: an authenticator
app, and a passkey or security key. At least one must stay allowed, and the passkey
factor is offered only where passkeys work. A person who holds a factor that is no longer
allowed loses it once they hold one that is. Recovery codes are always the backup.

### Replacing recovery codes

On **My account > Account security**, **Replace recovery codes** replaces all of a
person's recovery codes, used or not, and shows the new ones once, with a copy and a
download button. warden requires a second factor proved just now: the session must have
been signed in with an authenticator app or a passkey (not a recovery code, a password
alone, single sign-on or an API key) within the last few minutes
(`warden.identity.mfa_challenge_ttl_seconds`). Otherwise nothing changes and the page
asks the person to sign out, sign in again with a second factor and try again. warden
emails a notice that the codes were replaced, never the codes.

Every change is audited by warden (`warden.identity.sign_in_settings_changed`,
`warden.identity.user.sign_in_method_changed`, `.sign_in_method_pending`,
`.sign_in_method_pending_cancelled`, `.sign_in_method_link_sent` and
`warden.identity.mfa.recovery_codes_regenerated`).

## Single sign-on

warden signs users in with one OpenID Connect provider (Microsoft Entra ID, Google,
Okta, Keycloak and others). Single sign-on does not create accounts: a user must
already exist, created or invited, before they can sign in through it.

An administrator sets it up on the **Single sign-on** page (`/admin/sso`, from the
**Single sign-on settings** button on the Users page). The page has numbered setup steps
for **Microsoft Entra ID**, **Google**, **Okta** or **Other OpenID Connect provider**,
chosen from the Provider list (saved with the settings; it is a label for the steps and
changes nothing about how sign-in works). They come from warden, with this instance's
own redirect URI, scopes and name filled in, each with a **Copy** button, and each value
to copy back from the provider's console is linked to the field it goes into (issuer
URL, client ID, client secret). The same steps in general:

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
sign-on off ends every session it issued.

Single sign-on cannot be switched off while an active user signs in only through it:
warden refuses, and the page says single sign-on is still on and lists those users, each
linked to their page, to move to another sign-in method first (and says so when the
default sign-in method is single sign-on only). While only suspended or invited users
sign in only through it, the page names them and asks for confirmation first.

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
own domains, which is not on the page. `SPARK__WARDEN__SSO__PROVIDER_PRESET` (`entra`,
`google`, `okta` or `generic`) is the provider the page's setup steps start on.

warden accepts only asymmetrically signed ID tokens (RS256, ES256, PS256), and checks
the state, nonce and PKCE of every sign-in. A provider behind a private certificate
authority needs `SSL_CERT_FILE` naming a bundle that holds that authority and the public
ones, mounted into the containers.

> Not run for this guide: a sign-in against a real provider. Spark's tests run single
> sign-on and the page's Test against synthetic providers, and a live test against a
> local OpenID provider is run by hand before a release.

## Upgrading to warden 0.2.0

Spark 0.3.0 moves to warden 0.2.0 (`cognisn-warden>=0.2.0,<0.3`). warden's own upgrade
steps are chapter 20 of its client guide, "Upgrading from 0.1.x"
([docs/guide/clients.md](https://github.com/Cognisn/warden/blob/v0.2.0/docs/guide/clients.md#20-upgrading-from-01x)).
For a Spark server:

1. **Remove `require_mfa: false`.** Before upgrading, take `warden.identity.require_mfa`
   out of `config.yaml`, the settings document and the environment
   (`SPARK__WARDEN__IDENTITY__REQUIRE_MFA`). With `false` the web, worker and bootstrap
   stop at start-up with warden's message: "warden.identity.require_mfa: MFA is always
   required for password sign-in in warden 0.2.0 and later; remove
   identity.require_mfa or set it to true". `true` still starts and does nothing; remove
   it too.
2. **Run `spark migrate`** as for any upgrade. It runs warden's revision `f1c7d24e8b93`
   after Spark's own chain. Nobody's sign-in method changes: users who signed in with a
   password or single sign-on follow the instance default, which starts as "password
   or single sign-on", and users who sign in only through single sign-on keep that as
   their own setting.
3. **Expect enrolment.** Users who signed in with a password alone are asked to set up
   an authenticator app at their next sign-in, straight after their password, and are
   shown their recovery codes once. Users who already have an authenticator app sign in
   as before. Users who sign in through single sign-on see no change.
4. **Single sign-on.** It can no longer be switched off while an active user signs in
   only through it ([Single sign-on](#single-sign-on)): move them first. A user moved
   to "single sign-on only" who cannot use it yet keeps their current method until the
   move completes, at their first single sign-on. With Microsoft Entra ID, add the optional claims `email` and `xms_edov` to the
   ID token and verify your email domains in Entra before upgrading, or users get the
   fixed failure (warden's chapter 20, step 7).

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
