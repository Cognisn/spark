# Identity: Administrators, Users, Single Sign-On and Mail

A Spark server signs people in through warden (`cognisn-warden`), which runs inside
the web and worker processes over the same PostgreSQL database. warden keeps the
users, sessions, roles, invitations, API keys and the audit trail; Spark provides the
screens. There is no separate identity service to deploy.

## The first administrator

A new server has no users. Create the first administrator once the stack is up,
giving the password on standard input:

```bash
docker compose -p spark -f deploy/compose.yaml run --rm -T web \
    identity create-admin --email admin@example.com < password.txt
```

It prints `administrator <id>, created, administrator role assigned`. Without `-T`
and the redirect it asks for the password twice instead. The password must be at
least 12 characters (`warden.identity.password_min_length`). Delete `password.txt`
afterwards.

The command is safe to run again: for an existing email address it keeps the
password, gives the administrator role if it is missing, and prints `already existed`.
That is also how to restore administrator rights to someone after the last other
administrator has gone.

`--link-user-guid GUID` binds an existing Spark user guid to the administrator, so
data made under it stays theirs. It is only for a server that ran a 0.3.0 pre-release
before identity existed, whose data belongs to one interim guid; a new server does
not need it, and personal users are moved [with a bundle](from-personal.md).

Then sign in at `https://spark.example.com/auth/sign-in`. Two-step verification is on
by default, so the first sign-in asks the administrator to add Spark to an
authenticator app (a QR code, or the key typed in) and shows ten recovery codes once.
Keep the recovery codes somewhere safe.

## Roles

| Role | Holds |
|---|---|
| `administrator` | Everything: instance settings and secrets, users, roles, invitations and API keys, the audit trail, usage and budgets, bundle import, and every user capability |
| `auditor` | The audit trail at `/audit` and its CSV and NDJSON export, and nothing else |
| `power_user` | A user's capabilities, plus scheduled actions, sub-agents and the instance-wide knowledge graph |
| `user` | Chat, debates and panels, memories, skills, their own knowledge graphs and files, the built-in tools a server offers, and their own API keys |

A user may hold several roles; their permissions add up. warden never lets the last
active administrator lose the role or be suspended.

On a server the system command, filesystem and email tools are not available to
anyone: they act on the host Spark runs on. MCP servers are granted to roles on the
instance settings page (each server lists its roles), and a user sees only the servers
their roles are granted. A scheduled action runs with its owner's roles as they are
when it runs.

## Inviting users

An administrator invites from **Account > Invitations** (`/admin/invitations`),
choosing the roles granted when the invitation is accepted. An invitation works once
and expires after seven days (`warden.identity.invitation_ttl_seconds`). Users,
their roles, suspension, password resets and second factor resets are managed from
**Account > Users**.

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

## API keys

Users create their own API keys at **Account > API keys** (`/account/api-keys`), with
no more than their own permissions. A key is sent as `Authorization: Bearer wdn_...`
and is never combined with a session cookie. Keys expire after at most 365 days
(`warden.identity.api_key_max_lifetime_days`). Requests made with an API key are
granted no MCP server.

## Single sign-on

warden signs users in with one OpenID Connect provider (Microsoft Entra ID, Google,
Okta, Keycloak and others). Single sign-on does not create accounts: a user must
already exist, created or invited, before they can sign in through it.

1. Register Spark with the provider as a web application:
   - redirect URI `https://spark.example.com/identity/auth/sso/callback`;
   - scopes `openid`, `email` and `profile`;
   - a client secret.
2. Put the client secret in the secrets store under `warden.sso.client-secret`
   ([placing a secret](secrets.md#placing-a-secret-in-the-store)).
3. Add to the env file:

   ```
   SPARK__WARDEN__SSO__ENABLED=true
   SPARK__WARDEN__SSO__ISSUER_URL=https://login.example.com/realms/example
   SPARK__WARDEN__SSO__CLIENT_ID=spark
   ```

   Optionally `SPARK__WARDEN__SSO__ALLOWED_EMAIL_DOMAINS=example.com` to accept only
   addresses in your own domains, and `SPARK__WARDEN__SSO__PROVIDER_PRESET` (`entra`,
   `google`, `okta` or `generic`), a label that changes nothing.
4. Restart web and worker: `docker compose -p spark -f deploy/compose.yaml up -d`.

The sign-in page then offers single sign-on, and an invitation can be accepted
through it. warden reads the provider's discovery document from
`<issuer>/.well-known/openid-configuration`, accepts only asymmetrically signed ID
tokens (RS256, ES256, PS256), and checks the state, nonce and PKCE of every sign-in.
A provider behind a private certificate authority needs `SSL_CERT_FILE` naming a
bundle that holds that authority and the public ones, mounted into the containers.

Single sign-on settings come from the env file only in 0.3.0; Spark has no screen for
them.

> Not run for this guide: a sign-in against a real provider. Spark's tests run single
> sign-on against a fake provider, and a live test against a local OpenID provider
> is run by hand before a release.

## Mail

warden sends invitations, resends, password reset links, signed-out reset requests and
second factor notices by email once an SMTP server is set. Mail is set on the instance
settings page only (**Settings**, as an administrator, the Mail section), not through
`SPARK__` variables:

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

## Usage and token budgets

Administrators see usage by month, user and model at **Account > Usage and budgets**
(`/admin/usage`), export it as CSV, and set an optional monthly token budget per role
and per user. Users are warned from 80%, and new requests are refused at 100% until
the budget resets at 00:00 UTC on the first of the month. The
[container guide](../container.md#users-two-step-verification-and-single-sign-on)
describes the rules in full.

## The audit trail

warden keeps an append-only, hash-chained audit trail in `warden.audit_events`:
sign-ins and administration, and Spark's own events (settings and secret changes by
name, tool refusals, MCP calls, bundle imports, mail tests). Auditors and
administrators read it at `/audit` and export it. Details never carry secret values or
message content. Retention and its database grants are on the
[Database](database.md#wardens-audit-trail-and-its-purge) page.
