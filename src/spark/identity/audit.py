"""SPARK's own actions in warden's audit trail (server mode; A9, A12, A16).

warden audits its own identity and administration actions (sign-in, roles,
invitations, API keys). This module audits SPARK's: settings and secrets
changes, user preferences, flagged tool results and the tool result policy,
tool categories refused by role or mode, file downloads, exports, and refused
scheduled actions. Every action is registered on warden's recorder at identity
start-up (:func:`register_actions`, called by
:meth:`spark.identity.runtime.IdentityRuntime.start`), under ``spark.``, with a
severity and a summary that uses only warden's ``SUMMARY_FIELDS``.

Emitting an event from new code (wave 5 and later)::

    from spark.identity import audit

    # async code with a request (the actor and source address come from it):
    await audit.emit_request(request, audit.EXPORT_DOWNLOADED,
                             resource_type="bundle", resource_id=str(bid),
                             details={"kind": "bundle"})
    # async code with the user's SPARK guid:
    await audit.emit(audit.FILE_DOWNLOADED, user_guid=guid, resource_type="file", ...)
    # synchronous code on any thread (scheduled on the database's loop):
    audit.submit(audit.ACTION_REFUSED, "failure", user_guid=guid, ...)
    # inside a SPARK transaction, so both commit or roll back together (raises):
    async with database.transaction() as conn:
        ...
        await audit.record_in(conn, audit.SETTING_CHANGED, user_guid=guid, ...)

A new action is a new :data:`ACTIONS` entry. In personal mode nothing is
installed and every call is a no-op.

**Actor.** The actor is the warden user id, as in warden's own events, so one
``actor`` filter on warden's audit API shows a person's whole trail. Code that
knows only a SPARK guid passes ``user_guid``; it is resolved through
``user_identities`` and kept in the details as ``user_guid``. A guid without a
warden user (the interim guid before it is linked, or ``operator`` for a change
made outside the web) is recorded as given.

**Details** never carry a value or content: :func:`scrub` drops every key that
names one (``value``, ``secret``, ``password``, ``token``, ``content``,
``text``, ``snippet``, ``body``, ``message``, ``prompt``) at any depth, so a
caller's mistake cannot put a secret or a message into the trail.

**Failures.** :func:`emit` and :func:`submit` log and never raise: auditing
never breaks a user's request or a turn. :func:`record_in` raises, because a
joined write must fail with its audit row.

**Threads.** The settings, untrusted-content and tool-refusal hooks are
synchronous and called on the event loop or on worker threads. :meth:`SparkAudit.submit`
never blocks: on the database's loop it creates a task, from any other thread
it schedules the coroutine with ``run_coroutine_threadsafe``. :meth:`SparkAudit.drain`
waits for whatever was scheduled (shutdown, tests).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import re
import threading
from collections.abc import AsyncIterator, Coroutine, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from warden.audit import ActionRegistry, AuditEvent, Outcome, Severity, record_audit

from spark.database import user_identities
from spark.db.engine import Connection, Database, Executor

if TYPE_CHECKING:
    from starlette.requests import Request

    from spark.instance_settings.audit import SettingChange
    from spark.tools.registry import ToolRefusal

__all__ = [
    "ACTIONS",
    "ACTION_REFUSED",
    "EXPORT_DOWNLOADED",
    "FILE_DOWNLOADED",
    "PREFERENCE_CHANGED",
    "SECRET_CHANGED",
    "SETTING_CHANGED",
    "TOOL_REFUSED",
    "TOOL_RESULT_FLAGGED",
    "TOOL_RESULT_POLICY_CHANGED",
    "ActionDef",
    "SparkAudit",
    "current",
    "emit",
    "emit_request",
    "installed",
    "record_in",
    "register_actions",
    "scrub",
    "submit",
    "USAGE_REFUSED",
    "USAGE_VIEWED",
    "USAGE_BUDGET_CHANGED",
]

logger = logging.getLogger(__name__)

SETTING_CHANGED: Final = "spark.settings.instance.changed"
SECRET_CHANGED: Final = "spark.settings.secret.changed"
PREFERENCE_CHANGED: Final = "spark.settings.preference.changed"
TOOL_RESULT_FLAGGED: Final = "spark.tool_result.flagged"
TOOL_RESULT_POLICY_CHANGED: Final = "spark.tool_result.policy_changed"
TOOL_REFUSED: Final = "spark.tool.refused"
FILE_DOWNLOADED: Final = "spark.file.downloaded"
EXPORT_DOWNLOADED: Final = "spark.export.downloaded"
ACTION_REFUSED: Final = "spark.action.refused"
USAGE_REFUSED: Final = "spark.usage.refused"
USAGE_VIEWED: Final = "spark.usage.viewed"
USAGE_BUDGET_CHANGED: Final = "spark.usage.budget_changed"
KNOWLEDGE_BUILD_REFUSED: Final = "spark.knowledge.build.refused"


@dataclass(frozen=True, slots=True)
class ActionDef:
    """A SPARK audit action: its severity and its summary (``SUMMARY_FIELDS`` only)."""

    severity: Severity
    summary: str


# The SPARK action catalogue, registered on warden's recorder at identity start-up.
ACTIONS: Final[Mapping[str, ActionDef]] = {
    SETTING_CHANGED: ActionDef(
        Severity.NOTICE, "{actor} changed instance setting {resource_id}: {outcome}"
    ),
    SECRET_CHANGED: ActionDef(Severity.NOTICE, "{actor} changed secret {resource_id}: {outcome}"),
    PREFERENCE_CHANGED: ActionDef(
        Severity.INFO, "{actor} changed preference {resource_id}: {outcome}"
    ),
    TOOL_RESULT_FLAGGED: ActionDef(
        Severity.WARNING, "result of tool {resource_id} flagged for {actor} (possible injection)"
    ),
    TOOL_RESULT_POLICY_CHANGED: ActionDef(
        Severity.NOTICE, "{actor} changed the tool result action {resource_id}"
    ),
    TOOL_REFUSED: ActionDef(Severity.NOTICE, "tool {resource_id} refused for {actor}"),
    FILE_DOWNLOADED: ActionDef(Severity.INFO, "{actor} downloaded file {resource_id}"),
    EXPORT_DOWNLOADED: ActionDef(Severity.INFO, "{actor} exported {resource_type} {resource_id}"),
    ACTION_REFUSED: ActionDef(
        Severity.WARNING, "scheduled action {resource_id} of {actor} refused"
    ),
    USAGE_REFUSED: ActionDef(
        Severity.NOTICE, "request of {actor} refused: monthly token budget {resource_id} used"
    ),
    USAGE_VIEWED: ActionDef(Severity.INFO, "{actor} viewed usage {resource_id}"),
    USAGE_BUDGET_CHANGED: ActionDef(
        Severity.NOTICE, "{actor} changed the {resource_type} token budget {resource_id}"
    ),
    KNOWLEDGE_BUILD_REFUSED: ActionDef(
        Severity.WARNING, "knowledge graph build of {resource_id} for {actor} refused"
    ),
}


def register_actions(registry: ActionRegistry) -> None:
    """Register every SPARK action on warden's registry (idempotent)."""
    for name, spec in ACTIONS.items():
        registry.register(name, severity=spec.severity, summary=spec.summary)


# Keys that name a value or content: never recorded, at any depth.
_FORBIDDEN = re.compile(
    r"value|secret|password|passphrase|token|content|text|snippet|body|message|prompt",
    re.IGNORECASE,
)


def scrub(details: Mapping[str, Any]) -> dict[str, Any]:
    """``details`` without any key that names a value or content, at any depth."""
    return {
        str(key): _scrubbed(item)
        for key, item in details.items()
        if not _FORBIDDEN.search(str(key))
    }


def _scrubbed(item: Any) -> Any:
    if isinstance(item, Mapping):
        return scrub(item)
    if isinstance(item, (list, tuple)):
        return [_scrubbed(i) for i in item]
    if item is None or isinstance(item, (str, int, float, bool)):
        return item
    return str(item)


class SparkAudit:
    """Records SPARK's audit events through warden's recorder, over one database."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._loop = database.loop
        self._pending: set[concurrent.futures.Future[Any] | asyncio.Task[Any]] = set()
        self._lock = threading.Lock()

    # -- recording ------------------------------------------------------------

    async def record(
        self,
        action: str,
        outcome: Outcome = "success",
        *,
        user_guid: str | None = None,
        actor: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: Mapping[str, Any] | None = None,
        source_ip: str | None = None,
        conn: Connection | None = None,
    ) -> int:
        """Record one event; with ``conn``, inside that SPARK transaction. Raises on failure."""
        executor: Executor = conn if conn is not None else self._database
        if actor is None and user_guid is not None:
            actor = await user_identities.warden_user_for(executor, user_guid) or user_guid
        payload = scrub(details or {})
        if user_guid is not None:
            payload["user_guid"] = user_guid
        event = AuditEvent(
            action=action,
            outcome=outcome,
            actor=actor,
            resource_type=resource_type,
            resource_id=resource_id,
            source_ip=source_ip,  # type: ignore[arg-type]  # pydantic parses the text
            details=payload,
        )
        return await record_audit(event, conn=conn.raw if conn is not None else None)

    async def emit(self, action: str, outcome: Outcome = "success", **fields: Any) -> int | None:
        """Record one event; log and return None on failure, never raise."""
        try:
            return await self.record(action, outcome, **fields)
        except Exception as exc:  # noqa: BLE001 - auditing never breaks the caller
            # The type only: a driver's message can carry connection details.
            logger.error("Could not record audit event %s (%s)", action, type(exc).__name__)
            return None

    def submit(self, action: str, outcome: Outcome = "success", **fields: Any) -> None:
        """Schedule :meth:`emit` from any thread without waiting; never raises."""
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            if running is self._loop:
                task = self._loop.create_task(self.emit(action, outcome, **fields))
                self._track(task)
                return
            if self._loop.is_closed():
                logger.warning("Audit event %s dropped: the database loop is closed", action)
                return
            coro: Coroutine[Any, Any, int | None] = self.emit(action, outcome, **fields)
            try:
                future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            except RuntimeError:
                coro.close()
                logger.warning("Audit event %s dropped: the database loop is closed", action)
                return
            self._track(future)
        except Exception as exc:  # noqa: BLE001 - never raises to a hook's caller
            logger.error("Could not schedule audit event %s (%s)", action, type(exc).__name__)

    def _track(self, item: concurrent.futures.Future[Any] | asyncio.Task[Any]) -> None:
        with self._lock:
            self._pending.add(item)
        item.add_done_callback(self._forget)

    def _forget(self, item: Any) -> None:
        with self._lock:
            self._pending.discard(item)

    async def drain(self, timeout: float | None = None) -> None:
        """Wait (on the database's loop) for every event scheduled so far."""
        with self._lock:
            items = list(self._pending)
        if not items:
            return
        waiting = [
            asyncio.wrap_future(i) if isinstance(i, concurrent.futures.Future) else i for i in items
        ]
        await asyncio.wait(waiting, timeout=timeout)

    # -- the hooks wave 2 left --------------------------------------------------

    def setting_changed(self, change: SettingChange) -> None:
        """The settings hook (``spark.instance_settings.audit.set_audit_hook``)."""
        action, fields = _setting_event(change)
        self.submit(action, **fields)

    async def setting_changed_in(self, conn: Connection, change: SettingChange) -> None:
        """The settings transaction hook: the audit row joins the write's transaction."""
        action, fields = _setting_event(change)
        await self.record(action, conn=conn, **fields)

    def untrusted_event(self, event: str, details: Mapping[str, Any]) -> None:
        """The untrusted-content hook (``spark.safety.untrusted.set_audit_hook``)."""
        if event == "tool_result.flagged":
            self.submit(
                TOOL_RESULT_FLAGGED,
                user_guid=_text(details.get("user_guid")),
                resource_type="tool",
                resource_id=_text(details.get("tool_name")),
                details={
                    k: details.get(k)
                    for k in ("source", "action", "categories", "severity", "conversation_id")
                },
            )
        elif event == "tool_result.policy_changed":
            self.submit(
                TOOL_RESULT_POLICY_CHANGED,
                user_guid=_text(details.get("actor")),
                resource_type="setting",
                resource_id=_text(details.get("key")),
                details={k: details.get(k) for k in ("source", "action", "previous")},
            )
        else:
            logger.debug("No SPARK audit action for untrusted-content event %s", event)

    def tool_refused(self, refusal: ToolRefusal) -> None:
        """The tool refusal hook (``spark.tools.registry.set_refusal_hook``)."""
        self.submit(
            TOOL_REFUSED,
            "failure",
            user_guid=refusal.user_guid,
            resource_type="tool",
            resource_id=refusal.tool_name,
            details={"category": refusal.category, "reason": refusal.reason},
        )

    # -- installation -----------------------------------------------------------

    def install(self) -> None:
        """Connect every hook to this recorder and make it the process's (server mode)."""
        from spark.instance_settings import audit as settings_audit
        from spark.safety import untrusted
        from spark.tools import registry

        global _current
        _current = self
        settings_audit.set_audit_hook(self.setting_changed)
        settings_audit.set_transaction_hook(self.setting_changed_in)
        untrusted.set_audit_hook(self.untrusted_event)
        registry.set_refusal_hook(self.tool_refused)

    def uninstall(self) -> None:
        """Disconnect every hook (if still this recorder's)."""
        from spark.instance_settings import audit as settings_audit
        from spark.safety import untrusted
        from spark.tools import registry

        global _current
        if _current is not self:
            return
        _current = None
        settings_audit.set_audit_hook(None)
        settings_audit.set_transaction_hook(None)
        untrusted.set_audit_hook(None)
        registry.set_refusal_hook(None)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _setting_event(change: SettingChange) -> tuple[str, dict[str, Any]]:
    if change.secret:
        action = SECRET_CHANGED
    elif change.scope == "user":
        action = PREFERENCE_CHANGED
    else:
        action = SETTING_CHANGED
    return action, {
        "user_guid": change.user_guid or change.actor,
        "resource_type": "setting",
        "resource_id": change.key,
        "details": {"scope": change.scope, "change": change.action},
    }


_current: SparkAudit | None = None


def current() -> SparkAudit | None:
    """The installed recorder (server mode), or None."""
    return _current


@contextlib.asynccontextmanager
async def installed(database: Database) -> AsyncIterator[SparkAudit]:
    """Install SPARK's audit hooks over ``database`` for a block (a lifespan, an active worker).

    warden's recorder must already be installed (``IdentityRuntime.start``). On
    exit the hooks are removed and what was scheduled is given a few seconds.
    """
    audit = SparkAudit(database)
    audit.install()
    try:
        yield audit
    finally:
        audit.uninstall()
        await audit.drain(timeout=5)


async def emit(action: str, outcome: Outcome = "success", **fields: Any) -> int | None:
    """Record an event through the installed recorder; a no-op in personal mode. Never raises."""
    audit = _current
    if audit is None:
        return None
    return await audit.emit(action, outcome, **fields)


def submit(action: str, outcome: Outcome = "success", **fields: Any) -> None:
    """:func:`emit` from synchronous code on any thread, without waiting. Never raises."""
    audit = _current
    if audit is not None:
        audit.submit(action, outcome, **fields)


async def record_in(
    conn: Connection, action: str, outcome: Outcome = "success", **fields: Any
) -> int | None:
    """Record inside SPARK's transaction ``conn`` (both commit or roll back); raises on failure.

    A no-op returning None in personal mode.
    """
    audit = _current
    if audit is None:
        return None
    return await audit.record(action, outcome, conn=conn, **fields)


async def emit_request(
    request: Request, action: str, outcome: Outcome = "success", **fields: Any
) -> int | None:
    """:func:`emit` for the request's signed-in user, with the request's source address."""
    audit = _current
    if audit is None:
        return None
    from spark.identity.access import current_identity

    identity = current_identity(request)
    if identity is not None:
        fields.setdefault("user_guid", identity.user_guid or None)
        if identity.warden_user_id:
            fields.setdefault("actor", identity.warden_user_id)
    runtime = getattr(request.app.state, "identity_runtime", None)
    if runtime is not None and "source_ip" not in fields:
        from spark.identity.web import source_ip

        address = source_ip(runtime, request)
        fields["source_ip"] = None if address is None else str(address)
    return await audit.emit(action, outcome, **fields)
