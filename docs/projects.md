# Projects

A project gathers conversations around one subject, with instructions and files every
conversation in it receives. Projects work in personal and server mode; sharing,
members and transfer are server mode only.

## What a project is

A project has a **name**, a **description**, **instructions** (plain text or Markdown),
**resources** (files), **conversations** and **sub-projects**. A sub-project has its own
instructions and resources. Sub-projects nest up to **five levels**: the top-level project
is level 1, so a project at level 5 cannot hold a sub-project. Creating or moving a
project deeper than that is refused with a message saying so.

Limits: a name up to 200 characters, a description up to 2,000, instructions up to
50,000.

## What the model receives

A conversation in a project receives, in its system prompt, after the instance's global
instructions and before the conversation's own instructions:

- the **instructions of every level** from the top-level project down to the
  conversation's own level, each labelled with its level and project name;
- the rule that **the nearest level wins**: where instructions conflict, a deeper level
  refines or overrides a higher one, and the conversation's own instructions come last;
- the **resources of every level**, listed by name with their file id, type and size.
  They are not loaded: the model reads one with `read_my_file` and its id when it needs
  it, and `list_my_files` lists them by level.

The project context is read afresh at every turn, so a conversation moved to another
level takes that level's instructions and resources from its next turn. Project and
file names are labels people chose, so they are cleaned before they reach the model
(one line, no quotes or markup), and a file's content always arrives marked as
untrusted content.

Sub-agents a turn spawns, debates and panels do not receive the project context: a
sub-agent works from the task it is given, and debates and panels cannot be placed in a
project.

## Sharing (server mode)

Only **top-level projects** are shared; a sub-project is shared through its top-level
project. The owner shares with each person at a **role** and a **scope**:

| Role | May |
|---|---|
| Read-only | view the description, instructions, resources, sub-projects and the conversations they can see |
| Contributor | also start conversations anywhere in the project, and add resources (removing only those they added) |
| Owner | also edit the project, create, move and delete sub-projects, manage members, transfer and delete |

| Scope | Shows |
|---|---|
| Project only | the project itself; its conversations stay private to whoever started each |
| Project and all conversations | every conversation in the project and its sub-projects, now and later, whoever started it, at the person's role |

"Project and all conversations" is live: a conversation started tomorrow is visible as
soon as it exists. Changing someone to "project only" returns the conversations to
private for whoever started them at once (their open views close), except conversations
shared with them individually. The project's owner sees every conversation in the
project, as a Contributor.

Whoever started a conversation still manages it (its settings, its name, sharing it):
seeing it through a project never allows that. Each turn runs as the person who sends it,
with their own models, tools, budget and usage, as in any shared conversation, and a
conversation others see through its project gets no personal memories or knowledge
graphs.

**The share limit.** A conversation in a project can be shared individually, or
transferred, only to people who already have access to the project (its owner and
members). Spark refuses anyone else, whatever the screen offers.

**Removing a member** ends their access to the project and to its conversations they
did not start, including individual shares of them. The conversations they started stay
theirs and stay in the project, but they no longer receive the project's instructions or
resources in them. The resources they added stay in the project.

## Storage and quota

Resources live in the **project owner's file area** and count against the owner's quota,
under the normal upload limit, including those Contributors add: an upload the owner's
storage cannot hold is refused (507) whoever sends it. Members open resources through the
project's access check (`GET /projects/api/<project>/resources/<file>`). A resource
removed, or deleted with its project, frees the owner's quota. In personal mode resources
live in the personal file area.

## Moving conversations

Only someone who **owns both the conversation and the destination** can move it: from no
project into a project, from a project to no project, or between levels of the same
project. A conversation moves between different projects through "no project". Moving
your own conversation out of someone else's project needs only the conversation.

Before a move changes who can see the conversation, Spark reports who would gain sight
of it (or a better role) and who would lose it, and moves nothing until the move is
confirmed. Individual shares with people who have no access to the destination project
end with the move, and the report names them.

## Deleting

Deleting a project deletes its sub-projects and their resources, and the owner chooses
what happens to the conversations:

- **Keep conversations**: a sub-project's conversations (those of the sub-projects below
  it too) move up to its parent; a top-level project's leave to no project, private to
  whoever started each and no longer shared through the project (individual shares
  stay).
- **Delete everything**: every conversation in the deleted projects is deleted,
  including other members' conversations. Each person whose conversations were deleted
  is told in Spark and, when a mail sender is configured, by email.

The confirmation shows, from a preview, how many sub-projects, conversations (and how
many of them other people's), resources and members are affected.

## Transfer (server mode)

The owner can make a member, or someone added in the same step, the owner. The new owner
takes the project, its sub-projects and its resources, with their storage usage: the
transfer is refused when the new owner's quota cannot hold them. The old owner stays as a
Contributor with "project and all conversations"; conversations stay with whoever
started them. The new owner is told in Spark and by email.

## Notices

What a person is told about a project (a deletion that took their conversations, a
transfer to them) waits in Spark until they have seen it (`GET /projects/api/notices`,
`POST /projects/api/notices/seen`). A share is announced by email only.

## Audit (server mode)

Every change is recorded in the audit trail with the person who made it and the project
as the resource, never with names, instructions or content:

`spark.project.created`, `spark.project.updated`, `spark.project.moved`,
`spark.project.member_added`, `spark.project.member_changed`,
`spark.project.member_removed`, `spark.project.resource_added`,
`spark.project.resource_removed`, `spark.project.conversation_moved`,
`spark.project.deleted`, `spark.project.transferred`. A resource download is
`spark.file.downloaded`.

## Bundles

A personal user's export carries their projects (bundle format version 2): the tree,
instructions, descriptions, resources and where each conversation sits. Imported into a
server account, the projects belong to that user and are shared with nobody. See
[User Bundles](bundles.md).

## The API

All routes are under `/projects/api`, answer JSON, and need the session's CSRF token and
the public origin for every change. A project or conversation you may not reach answers
404; a role that does not allow the change answers 403; a change the rules refuse answers
400 with the reason.

| Route | Does |
|---|---|
| `GET /projects/api` | your projects as trees: `owned` and `shared`, each node with `children` and the `conversations` you can see |
| `POST /projects/api` | create: `name`, `description`, `instructions`, `parent_id` for a sub-project |
| `GET /projects/api/{id}` | one project with its `path`, `sub_projects`, `conversations`, `resources` and, for the owner, `members` |
| `PATCH /projects/api/{id}` | change `name`, `description`, `instructions` |
| `POST /projects/api/{id}/move` | move a sub-project under another project of its tree: `parent_id` |
| `GET`, `POST /projects/api/{id}/resources` | list; upload (multipart `file`) |
| `GET`, `DELETE /projects/api/{id}/resources/{file_id}` | download; remove |
| `GET`, `POST /projects/api/{id}/members` | list; share or change: `user_id` (a person) or `user_guid` (a member), `role` (`contributor` or `viewer`), `scope` (`project_only` or `all_conversations`) |
| `DELETE /projects/api/{id}/members/{user_guid}` | remove a member; your own guid leaves |
| `POST /projects/api/conversations/{conversation_id}/move` | `project_id` (or null) and `confirm`; 409 with `widened`, `narrowed` and `shares_removed` when it needs confirming |
| `GET /projects/api/conversations/{conversation_id}/context` | the conversation's project `path` and inherited resources by `levels` |
| `GET /projects/api/{id}/delete-preview` | the counts for the confirmation |
| `POST /projects/api/{id}/delete` | `mode`: `keep` or `everything` |
| `POST /projects/api/{id}/transfer` | `user_guid` (a member) or `user_id` (added in the same step); 507 over quota |
| `GET /projects/api/notices`, `POST /projects/api/notices/seen` | your unseen notices; mark them seen |

A new conversation starts in a project with `project_id` on `POST /conversations/api/create`.
