# Connections

A **connection** is one named model service that Spark uses: an Anthropic account, an
Amazon Bedrock account in one Region, an Ollama server, and so on. Every model call in
Spark goes through a connection, and every connection goes through
[conduit](https://pypi.org/project/cognisn-conduit/) (`cognisn-conduit`), which owns the
providers, the model catalogue and the residency checks.

- A connection has a **name** you choose, the **service's settings**, an optional or
  required **Region**, a **residency policy** and the **models** it offers.
- **Several connections of one service** can sit side by side: a Bedrock connection in
  Sydney and another in London, or a work and a personal Anthropic key.
- A model is chosen as a pair, the connection and the model, kept as
  `<connection id>/<model id>` (for example `sydney-bedrock/anthropic.claude-sonnet-5`).
  The model picker groups models by connection.
- Credentials are **write-only**. A key typed into Spark goes straight to the secrets
  store (the OS keychain in personal mode, konfig's store on a server), and the
  connection keeps only a `secret://` reference to it. No page ever shows a credential
  again: each is shown as **Set** or **Not set**.

Where connections live:

| Mode | Screen | Stored in |
|---|---|---|
| Personal | **Settings > Connections** (the first section of Settings) | `config.yaml`'s `connections` list |
| Server | **Account > Connections** (`/admin/connections`), and a link on the dashboard | the database (`connections`, `connection_models`) |

On a server the Connections page needs the `spark.admin.connections` permission, which
the `administrator` role holds. Metering and assignment are server mode only (see
[Personal mode](#personal-mode) and [Metering](metering.md)).

conduit's [providers guide](https://github.com/Cognisn/conduit/blob/release/v0.1.0/docs/guides/providers.md)
is the reference for each service's authentication, residency, environment variables
and logging. This page covers what Spark shows and stores.

## The services

**Add connection** opens a list of nine services. Choosing one opens a form with that
service's fields.

| Service | conduit kind | Region | Default residency | Model discovery |
|---|---|---|---|---|
| Amazon Bedrock | `bedrock` | Required | Local and in-Region, in its Region | Yes |
| Claude through Amazon Bedrock (mantle) | `bedrock_mantle` | Required | Local and in-Region, in its Region | Yes |
| Anthropic | `anthropic` (direct) | Optional | Any, global included | Yes |
| Claude in Microsoft Foundry | `anthropic` (Foundry endpoint) | Optional | Local and in-Region, in its Region | No |
| OpenAI | `openai_compat` (`openai`) | Optional | Any, global included | Yes |
| X.AI | `openai_compat` (`xai`) | Optional | Any, global included | Yes |
| Microsoft Foundry (OpenAI-compatible) | `openai_compat` (`foundry`) | Optional | Local and in-Region, in its Region | No |
| Google Gemini | `gemini` | Optional | Any, global included | Yes |
| Ollama | `ollama` | Optional | Local only | Yes |

Every connection form starts with **Name**. The connection's id is made from the name
(lower case letters, digits and hyphens, for example `Bedrock (Sydney)` becomes
`bedrock-sydney`) and cannot change afterwards; the name can. A connection's service
cannot change either: add a new connection instead.

Saving validates the settings through conduit's provider registry before anything is
written. If conduit refuses them, the form shows conduit's reason, which names fields
and never a value.

### Amazon Bedrock

| Field | Notes |
|---|---|
| Region | Required, for example `ap-southeast-2`. The Region bedrock-runtime is called in, and the Region the residency policy checks. |
| Allow long-term Bedrock API keys | Off by default. AWS recommends long-term keys for exploration only, so they are offered only once this is ticked. |
| Authentication | One of the modes below. |
| Endpoint URL | Optional. An https endpoint in place of the regional one (an interface VPC endpoint). Model discovery is unsupported while it is set. |

| Authentication | Fields |
|---|---|
| The AWS credential chain (default) | AWS profile (optional). Uses the environment, a shared profile, SSO, or a container or instance role. |
| AWS access keys | Access key ID, Secret access key, Session token (optional). |
| Short-term Bedrock API keys (generated) | Generate keys from the AWS credential chain (with an optional profile) or from AWS access keys. conduit generates a key, sends it as a bearer token and replaces it before it expires. |
| Long-term Bedrock API key | Bedrock API key. Shown only when long-term keys are allowed. |

The credentials need `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`,
and for model discovery `bedrock:ListFoundationModels` and
`bedrock:ListInferenceProfiles`. Request access to the models you want in the Bedrock
console first.

### Claude through Amazon Bedrock (mantle)

Claude through the Anthropic Messages API on Bedrock's mantle endpoint, which runs each
request in the Region it is sent to. It is the route to single-Region Claude where
bedrock-runtime offers only inference profiles.

| Field | Notes |
|---|---|
| Region | Required. |
| Allow long-term Bedrock API keys | As for Amazon Bedrock. |
| Authentication | Short-term Bedrock API keys (generated, the default), from the AWS credential chain or AWS access keys; or a long-term Bedrock API key when allowed. |
| Endpoint URL | Optional, as for Amazon Bedrock. |

### Anthropic

| Field | Notes |
|---|---|
| API key | Required. From the [Anthropic Console](https://console.anthropic.com/). |
| Base URL | Optional. A gateway in front of the API (https; plain http only to this machine). |
| Region | Optional. |

### Claude in Microsoft Foundry

| Field | Notes |
|---|---|
| Resource endpoint | Required: `https://<resource>.services.ai.azure.com/anthropic/`. |
| Authentication | API key, or Microsoft Entra ID. |
| API key | With API key authentication. |
| Managed identity client ID | Optional, with Microsoft Entra ID: a user-assigned managed identity. Otherwise azure-identity's default credential (environment, workload identity, managed identity, developer sign-in) is used. |
| Region | Optional, for example `australiaeast`. Set it so the residency policy can check it. |

### OpenAI

| Field | Notes |
|---|---|
| API key | Required. |
| Base URL | Optional. Defaults to OpenAI's API. |
| Organisation, Project | Optional. Sent only when set. |
| Region | Optional. |

"OpenAI" here is the OpenAI API: signing in with a ChatGPT subscription is not
supported.

### X.AI

| Field | Notes |
|---|---|
| API key | Required. From the [X.AI Console](https://console.x.ai/). |
| Base URL | Optional. Defaults to X.AI's API. |
| Region | Optional. |

### Microsoft Foundry (OpenAI-compatible)

| Field | Notes |
|---|---|
| Resource endpoint | Required: `https://<resource>.openai.azure.com/openai/v1/`. |
| Authentication | API key, or Microsoft Entra ID (with an optional managed identity client ID). |
| Catalogue | OpenAI models (default) or Grok models: which catalogue the deployments are declared under. |
| Region | Optional, for example `australiaeast`. |

### Google Gemini

| Field | Notes |
|---|---|
| API key | Required. From [Google AI Studio](https://aistudio.google.com/). Vertex AI is not served. |
| Base URL | Optional. An https proxy in front of the API. |
| Region | Optional. |

### Ollama

| Field | Notes |
|---|---|
| Base URL | `http://127.0.0.1:11434` by default. A remote server is allowed, but the URL may not carry credentials. |
| Context length | Optional. Caps the context window requested from Ollama. Left empty, the model's full window is requested, which needs memory on the Ollama host to match. |
| Region | Optional. |

Pull models on the Ollama host first (`ollama pull <model>`).

## Region and residency

A connection's **residency policy** says where its models may process requests. conduit
checks every model against it, both when the models view is built and on every call, so
a model outside the policy is never used.

Each model in conduit's catalogue is declared with a scope:

| Scope | Meaning |
|---|---|
| Local | Processed on the deployment's own hosts (Ollama). |
| In Region | Processed in the Region the connection calls (a Bedrock in-Region model id, a mantle model). |
| Cross Region | Processed anywhere in a geography (a Bedrock geographic inference profile such as `au.` or `eu.`). |
| Global | Processed anywhere (the direct APIs, Bedrock `global.` profiles). |

The form's **Residency** box starts at **Use this service's default**:

- Amazon Bedrock, mantle and both Microsoft Foundry services: local and in-Region
  processing, in the connection's own Region (any Region when a Foundry connection has
  none);
- Anthropic, OpenAI, X.AI and Google Gemini: every scope, global included;
- Ollama: local processing only.

Untick it to choose the scopes yourself and list the Regions requests may be processed
in (comma separated; leave it empty for any Region). A Bedrock connection in
`ap-southeast-2` that may use the Australian inference profiles, for example, allows
Local, In Region and Cross Region with the Regions
`ap-southeast-2, ap-southeast-4, ap-southeast-6`. A profile that may reach a Region
outside the list is still refused.

A model the policy forbids is listed as **Refused**, with conduit's reason (for example
`scope cross_region is not allowed (in_region, local)`), and cannot be enabled.

## Discovery

**Model discovery** asks the service which models the connection can reach, through
conduit. It runs after each save and whenever you press **Check reachable models** in
the models view. The list shows the result of the last run and when it ran.

- Each catalogue model is marked **Reachable**, **Not reachable** or **Not checked**.
- A failed run is shown in plain words from conduit's failure class (for example "the
  service timed out" or "the service refused the request (check the credentials)"),
  never the service's own error text.
- Discovery never makes a model usable: a model is offered only through the catalogue
  and the connection's enabled models.
- The last result is kept in the web process's memory only. After a restart, or on
  another server replica, the list says **Not run** and every model **Not checked**
  until discovery runs again.
- Discovery is unsupported for both Microsoft Foundry services (their model ids are
  deployment names) and for a Bedrock or mantle connection with an Endpoint URL.

> Not run for this guide: discovery against a real service account. Spark's tests run
> discovery through conduit's stub provider, and the release's live suite runs it
> against real services.

## The models view

**Models** on a connection lists every chat model conduit's catalogue (with Spark's own
declarations) has for the connection's service and Region, with its context window and
output limit, its capabilities (tools, vision, prompt caching, structured output), its
residency scope or refusal, an **Enabled** switch and a **Tier**.

### Enabling models

Until you change anything, a connection offers every catalogue model its residency
policy allows. Your first change (a switch or a tier) keeps every other model as it was
and stores the connection's choices from then on, so curating a connection never drops
models silently. Turn off the models you do not want to offer.

### Tiers

Each model is **Lower**, **Standard** (the default) or **Higher**. On a metered
connection the tier multiplies a call's credits (see [Metering](metering.md#tiers)). On
an unmetered connection, or in personal mode, the tier is shown in the model picker but
costs nothing.

### Declaring a model the catalogue lacks

A model discovery reaches but the catalogue does not declare is listed under
**Reachable but not in the catalogue**. This is always the case for Ollama models
beyond the few conduit declares. **Declare** opens a short form:

| Field | Notes |
|---|---|
| Kind | Chat or Embedding. |
| Context window | Required. Check it with the service: the model's maximum, or the `num_ctx` you set on Ollama. |
| Output limit | Required for a chat model. |
| Capabilities | Tools, vision, prompt caching, structured output. |
| Residency scope | Local for Ollama; otherwise where the model processes requests. |
| Regions | Comma separated, for an in-Region or cross-Region model. |

**Save to catalogue** checks the entry with conduit and stores it as a Spark catalogue
overlay entry for the connection's catalogue (for example `ollama`). It then appears in
the models view, marked **Declared in SPARK**, for every connection of that service.

> **Microsoft Foundry deployments.** Foundry model ids are deployment names, which
> discovery cannot list, so the declare form never appears for a Foundry connection,
> and with the default in-Region policy every public catalogue model is refused. In
> this release a Foundry connection offers no models from the screens. A deployment
> can be declared only through the API (`POST /api/connections/<id>/overlays`, with
> `model_id` set to the deployment name and the fields above). This is a known gap.
>
> Not run for this guide: declaring a deployment through the API against a real
> Foundry resource.

## Managing connections

The list shows each connection's name and id, service, Region, residency, enabled
models, last discovery and status (**Running**, **Disabled**, or **Failed** with the
reason). On a server it also shows whether the connection is metered and how many
assignments it has (**Administrators only** when none).

- **Edit** opens the same form. A credential field left empty keeps the stored
  credential; type a new one to replace it. A credential the connection no longer uses
  (after a change of authentication) is removed from the secrets store.
- **Disable** stops the connection's models being offered or used, without deleting
  anything. **Enable** brings it back.
- **Rename** changes the name only.
- **Delete** asks you to confirm, then removes the connection, its models, metering,
  assignments and credentials. It is refused while a scheduled action uses one of its
  models, naming the actions: change their model or delete them first.

A change takes effect without a restart. Spark rebuilds only the connection that
changed, and keeps a working connection running if its new settings fail. On a server
every web replica and the worker pick up the change.

On a server every change is recorded in the audit trail as
`spark.settings.instance.changed` (a credential as `spark.settings.secret.changed`),
with the connection and the names of the fields changed, never their values.

## Personal mode

Personal mode keeps the same connections, form and models view in **Settings >
Connections**, with one local user who can use every connection.

- No metering and no assignment: every enabled model of every enabled connection is
  offered, and nothing is ever stopped for usage.
- Credentials go to the OS keychain as `connections.<id>.<field>`.
- Connections are written to `config.yaml`. You can also edit it by hand while Spark is
  stopped:

```yaml
connections:
  - id: work-claude
    name: Work Claude
    settings:
      kind: anthropic
      api_key: secret://connections.work-claude.api_key
  - id: bedrock-sydney
    name: Bedrock (Sydney)
    settings:
      kind: bedrock
      region: ap-southeast-2
      auth: {mode: credential_chain, profile: default}
    residency: {scopes: [local, in_region], regions: [ap-southeast-2]}
  - id: home-ollama
    name: Home Ollama
    settings:
      kind: ollama
      base_url: http://127.0.0.1:11434
    models:
      - {id: "llama3.1:8b", enabled: true, tier: standard}
```

`settings` is conduit's settings for the service's kind (see conduit's providers
guide), with every credential a `secret://` reference. The three entries above were
checked through conduit's provider registry for this guide. `residency` is optional (the
service's default applies) and `models` is optional (every catalogue model is offered
until it is present). A connection's credential must already be in the keychain under
the name its reference gives; the form is the easy way to put it there.

## Upgrading from 0.2

### Personal mode

On the first start of 0.3.0, each provider in `config.yaml`'s 0.2 `providers` section
that is enabled, or has a key, becomes a named connection in a new `connections`
section, once:

| 0.2 provider | Connection id and name |
|---|---|
| `anthropic` | `anthropic`, Anthropic |
| `aws_bedrock` | `aws-bedrock`, Amazon Bedrock |
| `ollama` | `ollama`, Ollama |
| `google_gemini` | `google-gemini`, Google Gemini |
| `xai` | `xai`, X.AI |

- A key held as a `secret://` reference keeps that reference, so its keychain entry is
  used as it is. A key still in plain text in `config.yaml` is moved into the keychain
  (as `connections.<id>.<field>`) first.
- Bedrock's `sso` method becomes the AWS credential chain with its profile; `iam` and
  `session` with keys become AWS access keys. A Bedrock connection without a Region
  gets `us-east-1`.
- Each connection takes its service's default residency. A Bedrock connection is
  therefore in-Region, so a cross-Region or global inference profile used under 0.2 is
  refused until the connection's residency allows it.
- The `providers` section is removed and the conversion never runs again. The
  `context_limits` section is no longer read: context windows and output limits come
  from conduit's catalogue, and a model it lacks is declared from the models view.
- A conversation on a model the catalogue does not declare answers with a message
  saying so: choose another model, or declare it.

The conversion above was run for this guide on a synthetic `config.yaml` (Anthropic
with a keychain reference, Bedrock with SSO, Ollama, and a disabled Gemini provider
holding a plain-text key).

### Server mode

0.3.0 was never released, so a server that ran a 0.3.0 release candidate keeps nothing
from the providers settings: the `providers.*` instance settings and `models.offered`
are ignored (start-up lists the stored keys once in the log), and their credentials in
the secrets store are no longer read. Add each service again as a connection, choose
its models, and assign it. Token budgets from a release candidate are not converted to
credit allowances either; see [Metering](metering.md#upgrading-from-token-budgets).
