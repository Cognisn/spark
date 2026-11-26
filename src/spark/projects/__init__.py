"""Projects (PC6 to PC15): a name, a description, instructions and resources, holding
conversations and sub-projects up to five levels deep.

* :mod:`spark.projects.access`: the one access decision for a project (role and scope);
  a conversation in a project is decided by :func:`spark.database.scope.conversation_role`.
* :mod:`spark.projects.prompt`: the labelled instruction blocks and the resource list a
  conversation in a project receives in its system prompt.
* :mod:`spark.projects.notices`: what a person is told about a project, in SPARK and by mail.

The repositories are :mod:`spark.database.projects` and :mod:`spark.database.project_members`;
the routes :mod:`spark.web.endpoints.projects`.
"""

from __future__ import annotations

from spark.projects.access import (
    MAX_DEPTH,
    PROJECT_ALLOWS,
    ProjectAccess,
    ProjectNeed,
    ProjectRole,
    ShareScope,
    project_access,
    require_project,
)

__all__ = [
    "MAX_DEPTH",
    "PROJECT_ALLOWS",
    "ProjectAccess",
    "ProjectNeed",
    "ProjectRole",
    "ShareScope",
    "project_access",
    "require_project",
]
