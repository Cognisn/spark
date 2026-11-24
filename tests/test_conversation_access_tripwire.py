"""The access tripwire: every conversation route and query decides access in one place.

A conversation is shared (server mode), so "is it the caller's?" is no longer the
question: :mod:`spark.database.scope` decides what a person may do from their role,
and :func:`spark.web.scope.conversation_access` answers for a route. This test fails
when code checks conversation ownership another way:

* the helpers that only knew the owner (``owns_conversation``, ``require_conversation``,
  ``owned_conversation``) appear anywhere in ``src/spark``;
* SQL filters ``conversations`` by ``user_guid = ?``, or uses ``OWNED_CONVERSATIONS``,
  outside the modules listed in :data:`OWNER_PRIVATE`, each with the reason its data
  stays with the owner;
* a route that takes a conversation id (path, query, form or JSON) does not ask the
  access decision, unless :data:`ROUTE_EXCEPTIONS` names it with its reason.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import pkgutil
import re
from collections.abc import Iterable
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.routing import APIRoute

import spark
import spark.web.endpoints

SRC = Path(spark.__file__).parent

FORBIDDEN_NAMES = ("owns_conversation", "require_conversation", "owned_conversation")

# Modules whose conversation queries are the owner's alone, and why (CS31 and the design).
OWNER_PRIVATE: dict[str, str] = {
    "database/scope.py": "defines the access decision and its fragments",
    "database/conversation_members.py": "membership itself: a member's own rows, transfer",
    "database/conversations.py": "the owner's own list; rename, settings and delete are manage",
    "database/messages.py": "deleting a message is manage (the owner's)",
    "database/files.py": "deleting a conversation's files is manage (the owner's)",
    "database/debates.py": "debates and panels are not shared (design point 3)",
    "database/conversation_links.py": "linked conversations stay private to the owner (CS31)",
    "knowledge/resolve.py": "knowledge graph builds and context stay with the owner (CS31)",
    "web/endpoints/knowledge.py": "a conversation's knowledge graph is the owner's (CS31)",
    "bundles/exporter.py": "a user's bundle holds only the conversations they own",
}

# Routes that take a conversation id without asking the access decision, and why.
ROUTE_EXCEPTIONS: dict[str, str] = {
    "/debate/{conversation_id}": "the debate page shell; its data routes are owner-scoped",
    "/debate/api/prompt": "debates are not shared: owner-only through OWNED repositories",
    "/panel/{conversation_id}": "the panel page shell; its data routes are owner-scoped",
    "/panel/api/prompt": "panels are not shared: owner-only through OWNED repositories",
    "/voice/api/speak": "the id only attributes the caller's own voice usage; reads nothing",
    "/files/api/{file_id}": "a file id: another member's attachment through READABLE_CONVERSATIONS",
    "/knowledge/api/status": "lists the caller's own conversation graphs (owner-private, CS31)",
}

# A route's handler asks the access decision when its source names one of these.
ACCESS_CALLS = (
    "conversation_access(",
    "conversation_role(",
    "require_access(",
    "_shareable(",
    "conversation_members.leave(",
)

_CONVERSATIONS = re.compile(r"\bconversations\b")
_OWNER_FILTER = re.compile(r"\buser_guid\s*=\s*\?")
_TAKES_ID = re.compile(r"""["']conversation_id["']""")


def _sources() -> list[tuple[str, str]]:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith("migrations/"):
            continue
        found.append((rel, path.read_text(encoding="utf-8")))
    return found


def _strings(tree: ast.AST) -> list[str]:
    """Every string literal, an f-string's literal parts joined with a placeholder."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            out.append(
                "".join(
                    v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
                    for v in node.values
                )
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


def owner_checks(rel: str, text: str) -> list[str]:
    """What in one module checks conversation ownership outside the access decision."""
    problems = [f"{rel}: uses {name}" for name in FORBIDDEN_NAMES if name in text]
    if rel in OWNER_PRIVATE:
        return problems
    tree = ast.parse(text)
    for literal in _strings(tree):
        if _CONVERSATIONS.search(literal) and _OWNER_FILTER.search(literal):
            problems.append(f"{rel}: filters conversations by user_guid: {literal[:80]!r}")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Name, ast.alias)):
            name = node.id if isinstance(node, ast.Name) else node.name
            if name == "OWNED_CONVERSATIONS":
                problems.append(f"{rel}: uses OWNED_CONVERSATIONS")
    return problems


def all_routes() -> list[APIRoute]:
    """Every route of every endpoint router the app registers (paths with their prefix)."""
    found: list[APIRoute] = []
    package = spark.web.endpoints
    for info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        for value in vars(module).values():
            if isinstance(value, APIRouter):
                found.extend(r for r in value.routes if isinstance(r, APIRoute))
    unique = {(r.path, tuple(sorted(r.methods)), r.endpoint): r for r in found}
    return list(unique.values())


def unchecked_routes(routes: Iterable[APIRoute]) -> list[str]:
    """Routes that take a conversation id and do not ask the access decision."""
    problems = []
    for route in routes:
        source = inspect.getsource(route.endpoint)
        takes_id = "{conversation_id}" in route.path or _TAKES_ID.search(source)
        if not takes_id or route.path in ROUTE_EXCEPTIONS:
            continue
        if not any(call in source for call in ACCESS_CALLS):
            problems.append(f"{sorted(route.methods)} {route.path}")
    return problems


@pytest.fixture(scope="module")
def routes() -> list[APIRoute]:
    return all_routes()


def test_no_module_checks_ownership_another_way() -> None:
    problems = [p for rel, text in _sources() for p in owner_checks(rel, text)]
    assert problems == []


def test_every_conversation_route_asks_the_access_decision(routes: list[APIRoute]) -> None:
    assert unchecked_routes(routes) == []


def test_the_exceptions_are_real_routes(routes: list[APIRoute]) -> None:
    assert set(ROUTE_EXCEPTIONS) <= {r.path for r in routes}
    assert all((SRC / rel).exists() for rel in OWNER_PRIVATE)


def test_many_conversation_routes_are_checked(routes: list[APIRoute]) -> None:
    """The scan sees the routes it is meant to guard (a scan that sees none proves nothing)."""
    assert len([r for r in routes if "{conversation_id}" in r.path]) >= 20
    paths = {r.path for r in routes}
    assert {"/stream/chat", "/stream/conversation", "/files/api"} <= paths


class TestTheScannerItself:
    """Mutation checks of the tripwire: each way of checking ownership is caught."""

    def test_a_forbidden_helper(self) -> None:
        assert owner_checks("web/x.py", "if await owned_conversation(request, cid): ...")

    def test_an_inline_owner_filter(self) -> None:
        text = 'q = "SELECT * FROM conversations WHERE id = ? AND user_guid = ?"\n'
        assert owner_checks("database/x.py", text)
        text = 'q = f"SELECT name FROM conversations c WHERE c.user_guid = ? {x}"\n'
        assert owner_checks("database/x.py", text)

    def test_the_owner_fragment_outside_the_list(self) -> None:
        text = "from spark.database.scope import OWNED_CONVERSATIONS\n"
        assert owner_checks("database/x.py", text)
        assert owner_checks("database/debates.py", text) == []

    def test_other_tables_pass(self) -> None:
        text = 'q = "SELECT * FROM conversation_files WHERE user_guid = ?"\n'
        assert owner_checks("database/x.py", text) == []

    def test_a_route_without_the_decision(self) -> None:
        from fastapi import FastAPI, Request

        bare = FastAPI()

        @bare.get("/chat/{conversation_id}/api/leak")
        async def leak(request: Request, conversation_id: int) -> dict:
            return {"id": conversation_id}

        @bare.post("/thing")
        async def by_body(request: Request) -> dict:
            data = await request.json()
            return {"id": data.get("conversation_id")}

        assert len(unchecked_routes(r for r in bare.routes if isinstance(r, APIRoute))) == 2
