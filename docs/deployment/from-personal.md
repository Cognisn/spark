# Moving from Personal Mode

A personal installation (SQLite, one local user) cannot be turned into a server in
place. Instead, each person exports their own data as a **user bundle**, and a server
administrator imports it for that person's account. The bundle carries the person's
data and nothing of the instance it came from: no secret, no provider, no MCP server
and no other instance setting. [User Bundles](../bundles.md) describes the format and
every check in full; this page is the procedure.

## 1. The person exports their bundle

On their own computer, in personal mode, on Spark 0.3.0 or later:

- **Web interface**: **Settings**, then **Export my data**. The browser downloads
  `spark-bundle-<date>-<id>.zip`.
- **Command line**:

  ```bash
  spark export --output spark-bundle.zip
  ```

  It prints `Wrote spark-bundle.zip (bundle <id>, format version 1)` with what the
  bundle holds and what was left out, and never overwrites an existing file.

The bundle holds conversations (with their messages, files and settings), memories,
knowledge graphs, skills, scheduled actions, "Always" approvals, the file area and
preferences (default model, instructions, theme, voice). Every secret value Spark
holds, and anything shaped like a credential, is replaced by `[redacted credential]`;
a file holding one is left out and listed.

The person sends the bundle to the administrator over a channel fit for their data:
it contains every conversation they have had.

## 2. The administrator creates the account

The person needs a server account first: [invite them](identity.md#inviting-users)
and let them accept, with a password or through single sign-on. Give them the
roles they should have on the server; a bundle grants nothing.

## 3. The administrator imports it

Signed in as an administrator (only the Administrator role holds
`spark.bundles.import`):

1. Open **Account > Import a user bundle** (`/admin/bundles`).
2. Choose the person's account and the bundle file, and import.
3. Read the report on screen: rows imported per table, knowledge graph entities
   merged, actions imported disabled, skills kept, and what was left out and why.

The report is also kept in the person's files as `imports/<bundle id>/report.json`.

The upload is limited to 1 GB, so the [reverse proxy](reverse-proxy.md#what-the-proxy-must-do)
must accept bodies that large on `/admin/bundles`. Nothing is written unless the whole
bundle passes its checks (layout, sizes, hashes, format version, no credential), and
everything is written in one transaction with its audit record
(`spark.bundle.imported`).

## What changes on the way in

- **Scheduled actions arrive disabled.** The person reviews and enables each one. On a
  server, actions need the Power user or Administrator role.
- **What a server cannot run is left out and listed**: anything for the system
  command, filesystem and email tools, stdio MCP servers, and MCP servers this server
  does not offer.
- **The person's existing data wins**: a memory, approval, skill state or preference
  they already have on the server is kept.
- **Files** land in their file area under `imports/<bundle id>/` and count against
  their quota; an import over the quota is refused.
- **Skills** are kept in their file area under `imports/<bundle id>/.../skills/` and
  listed, because a server's skills are shared by everyone. An administrator can
  install one for all users.
- **Provider keys and MCP servers** do not travel. The server's administrator decides
  which providers, models and MCP servers are offered.

A bundle is imported once per person: importing the same bundle again for the same
account is refused and changes nothing.

## After the move

The person signs in to the server at its address from now on. Their personal
installation is untouched; they may keep it, or remove it once they are satisfied
everything arrived.

> Checked for this guide: `spark export --output` in personal mode in the release
> image. The import page was not used for this guide, since it needs a signed-in
> administrator in a browser; the bundle import is covered by Spark's tests on
> PostgreSQL (a seeded personal database exported and imported, compared row for row)
> and by the release's live suite.
