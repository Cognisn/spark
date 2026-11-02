"""Action runner — schedules and executes autonomous actions.

Personal mode (the tray daemon): the single local user's enabled actions, run as
that user.

Server mode (the worker): every user's enabled actions, each run as its owner.
Before each run the owner's standing is read from warden at run time (an
:class:`~spark.scheduler.owners.OwnerResolver`); an owner who may not run
actions is refused and the refusal recorded, and an allowed run gets the
owner's guid (memory, file area, run records) and current permissions (its
tools). A server never uses the process's own user guid.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

if TYPE_CHECKING:
    from spark.scheduler.owners import OwnerAccess, OwnerResolver

from spark.db.bridge import SyncDatabase

logger = logging.getLogger(__name__)


def _server() -> bool:
    """Whether this process serves many users (a process with no mode is personal)."""
    from spark import mode

    try:
        return mode.is_server()
    except RuntimeError:
        return False


class ActionRunner:
    """Loads enabled actions from the database and schedules them via APScheduler."""

    def __init__(
        self,
        ctx: Any,
        daemon_id: str,
        db: SyncDatabase,
        *,
        owners: OwnerResolver | None = None,
    ) -> None:
        """``owners`` resolves each action owner's standing in server mode; without
        it a server refuses every run (fail closed). Personal mode ignores it."""
        self._ctx = ctx
        self._daemon_id = daemon_id
        self._db = db
        self._owners = owners
        import tzlocal

        # Enable APScheduler logging to capture trigger/misfire events
        logging.getLogger("apscheduler").setLevel(logging.DEBUG)

        self._scheduler = BackgroundScheduler(
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                # Large grace time to handle macOS sleep/wake — if the system
                # sleeps through a scheduled time, the job should still fire
                # when the system wakes within this window.
                "misfire_grace_time": 7200,
            },
            timezone=tzlocal.get_localzone(),
        )
        self._poll_interval = 30
        self._poll_thread: threading.Thread | None = None
        self._running = False
        self._known_action_ids: set[int] = set()

    def _get_db(self) -> SyncDatabase:
        """The database bridge (owned and closed by whoever created the runner)."""
        return self._db

    def start(self) -> None:
        """Start the scheduler and begin polling for action changes."""
        if _server():
            logger.info("Action runner starting (every user's actions, each as its owner)")
        else:
            uid = self._local_user()
            logger.info(
                "Action runner starting (user_guid=%s)", uid[:8] + "..." if len(uid) > 8 else uid
            )

        self._scheduler.start()
        self._running = True

        # Small delay to ensure scheduler is fully started
        import time as _time

        _time.sleep(0.5)

        self._reload_all()

        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        logger.info("Action runner started, %d jobs scheduled", len(self._scheduler.get_jobs()))

    def stop(self) -> None:
        """Stop the scheduler and polling."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        self._scheduler.shutdown(wait=False)
        logger.info("Action runner stopped")

    def _poll_loop(self) -> None:
        """Poll for action changes."""
        while self._running:
            try:
                self._reload_all()
                # Wake up the scheduler to check for misfired jobs.
                # This is critical after macOS sleep/wake — APScheduler's
                # internal timer may not recover, but this nudge forces it
                # to re-evaluate pending triggers.
                self._scheduler.wakeup()
            except Exception as e:
                logger.error("Poll error: %s", e)
            time.sleep(self._poll_interval)

    def _local_user(self) -> str:
        """Personal mode's single local user (never asked for on a server)."""
        from spark.core.user_guid import get_user_guid

        return get_user_guid(self._ctx)

    def _enabled_actions(self, db: SyncDatabase) -> list[dict[str, Any]]:
        """The enabled actions to schedule, each with its owner in ``user_guid``."""
        from spark.database import autonomous_actions

        if _server():
            return db.call(autonomous_actions.get_enabled_actions_for_worker)
        user_guid = self._local_user()
        return [
            {**action, "user_guid": user_guid}
            for action in db.call(autonomous_actions.get_enabled_actions, user_guid)
        ]

    def _reload_all(self) -> None:
        """Reload all enabled actions from the database."""
        db = self._get_db()

        try:
            # Clear stale locks (locked for more than 30 minutes)
            self._clear_stale_locks(db)

            actions = self._enabled_actions(db)
            scheduled_ids = {job.id for job in self._scheduler.get_jobs()}
            current_action_ids = set()

            if actions:
                logger.info("Found %d enabled action(s):", len(actions))
                for action in actions:
                    schedule_info = action.get("schedule_config", "")
                    logger.info(
                        "  - '%s' (id=%d, type=%s, config=%s, model=%s)",
                        action.get("name"),
                        action.get("id"),
                        action.get("schedule_type"),
                        schedule_info,
                        action.get("model_id"),
                    )

            for action in actions:
                action_id = action["id"]
                user_guid = action["user_guid"]
                current_action_ids.add(action_id)
                job_id = f"action_{action_id}"

                if job_id not in scheduled_ids:
                    self._schedule_action(action, job_id, user_guid)
                else:
                    # Sync next_run_at in DB from scheduler's actual next fire time.
                    # This keeps the tray display accurate after sleep/wake or misfires.
                    job = self._scheduler.get_job(job_id)
                    if job and job.next_run_time:
                        next_utc = job.next_run_time.astimezone(timezone.utc)
                        stored = action.get("next_run_at", "")
                        if stored != next_utc.isoformat():
                            from spark.database import autonomous_actions as aa

                            db.call(
                                aa.update_action,
                                action_id,
                                user_guid,
                                next_run_at=next_utc.isoformat(),
                            )
                        logger.debug(
                            "  '%s' already scheduled, next: %s",
                            action.get("name"),
                            job.next_run_time,
                        )

            # Remove jobs for actions that no longer exist or are disabled
            for job in self._scheduler.get_jobs():
                if job.id.startswith("action_"):
                    try:
                        aid = int(job.id.split("_")[1])
                        if aid not in current_action_ids:
                            self._scheduler.remove_job(job.id)
                            logger.info("Unscheduled removed/disabled action (job %s)", job.id)
                    except (ValueError, IndexError):
                        pass

            self._known_action_ids = current_action_ids

        except Exception as e:
            logger.error("Failed to reload actions: %s", e)

    def _schedule_action(self, action: dict, job_id: str, user_guid: str) -> None:
        """Schedule a single action, to run as ``user_guid`` (its owner)."""
        schedule_type = action.get("schedule_type", "one_off")
        schedule_config_raw = action.get("schedule_config")

        if not schedule_config_raw:
            logger.warning("Action '%s' has no schedule_config, skipping", action.get("name"))
            return

        try:
            config = (
                json.loads(schedule_config_raw)
                if isinstance(schedule_config_raw, str)
                else schedule_config_raw
            )
        except json.JSONDecodeError as e:
            logger.error("Invalid schedule_config JSON for '%s': %s", action.get("name"), e)
            return

        try:
            if schedule_type == "recurring":
                cron = config.get("cron", "")
                if not cron:
                    logger.error(
                        "Action '%s' recurring schedule missing 'cron' field", action.get("name")
                    )
                    return

                parts = cron.split()
                if len(parts) < 5:
                    logger.error(
                        "Action '%s' cron '%s' needs 5 fields, got %d",
                        action.get("name"),
                        cron,
                        len(parts),
                    )
                    return

                import tzlocal

                local_tz = tzlocal.get_localzone()
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                    timezone=local_tz,
                )
                self._scheduler.add_job(
                    self._execute_action,
                    trigger=trigger,
                    id=job_id,
                    args=[action["id"], user_guid],
                    replace_existing=True,
                )

                # Calculate and store next_run_at in UTC for consistent DB storage
                next_fire = trigger.get_next_fire_time(None, datetime.now(local_tz))
                if next_fire:
                    from spark.database import autonomous_actions as aa

                    # Store as UTC in database
                    next_fire_utc = next_fire.astimezone(timezone.utc)
                    self._get_db().call(
                        aa.update_action,
                        action["id"],
                        user_guid,
                        next_run_at=next_fire_utc.isoformat(),
                    )

                local_str = next_fire.strftime("%Y-%m-%d %H:%M %Z") if next_fire else "N/A"
                logger.info(
                    "Scheduled recurring action: '%s' (cron: %s, next: %s)",
                    action["name"],
                    cron,
                    local_str,
                )

            elif schedule_type == "one_off":
                run_at = config.get("run_at")
                if not run_at:
                    logger.error(
                        "Action '%s' one-off schedule missing 'run_at'", action.get("name")
                    )
                    return

                trigger = DateTrigger(run_date=run_at)
                self._scheduler.add_job(
                    self._execute_action,
                    trigger=trigger,
                    id=job_id,
                    args=[action["id"], user_guid],
                    replace_existing=True,
                )

                from spark.database import autonomous_actions as aa

                self._get_db().call(aa.update_action, action["id"], user_guid, next_run_at=run_at)

                logger.info("Scheduled one-off action: '%s' at %s", action["name"], run_at)

            else:
                logger.warning(
                    "Unknown schedule_type '%s' for action '%s'", schedule_type, action.get("name")
                )

        except Exception as e:
            logger.error("Failed to schedule action '%s': %s", action.get("name"), e)

    def _clear_stale_locks(self, db: SyncDatabase) -> None:
        """Clear action locks older than 30 minutes to prevent stuck actions."""
        from spark.database import autonomous_actions

        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
            for row in db.call(autonomous_actions.clear_stale_locks, cutoff):
                logger.warning(
                    "Cleared stale lock on action '%s' (id=%d, locked_by=%s, locked_at=%s)",
                    row.get("name"),
                    row.get("id"),
                    row.get("locked_by"),
                    row.get("locked_at"),
                )
        except Exception as e:
            logger.debug("Stale lock check: %s", e)

    def _owner_access(self, user_guid: str) -> OwnerAccess:
        """The owner's standing now, from warden (server mode); refused when unknown."""
        from spark.scheduler.owners import OwnerAccess

        if self._owners is None:
            return OwnerAccess.refused(
                user_guid, "the worker cannot reach warden to check the action's owner"
            )
        try:
            return self._db.run(self._owners.resolve(user_guid))
        except Exception as e:  # noqa: BLE001 - an unknown standing is a refusal
            logger.error("Could not resolve the owner of an action (%s)", type(e).__name__)
            return OwnerAccess.refused(user_guid, "the action owner's standing could not be read")

    def _execute_action(self, action_id: int, user_guid: str | None = None) -> None:
        """Execute an action as ``user_guid``, its owner (the local user in personal mode)."""
        logger.info("Executing action %d", action_id)
        from spark.scheduler.executor import ActionExecutor

        if _server():
            if not user_guid:
                logger.error("Action %d has no owner; not run", action_id)
                return
            access = self._owner_access(user_guid)
            executor = ActionExecutor(
                self._ctx,
                self._daemon_id,
                self._db,
                permissions=access.permissions,
                user_guid=user_guid,
            )
            if access.refusal is not None:
                executor.refuse(action_id, access.refusal)
            else:
                executor.execute(action_id)
        else:
            user_guid = self._local_user()
            ActionExecutor(self._ctx, self._daemon_id, self._db).execute(action_id)

        # Update next_run_at and last_run_at after execution
        try:
            from spark.database import autonomous_actions as aa

            job = self._scheduler.get_job(f"action_{action_id}")
            if job and job.next_run_time:
                # Store as UTC for consistent DB storage
                next_utc = job.next_run_time.astimezone(timezone.utc)
                self._get_db().call(
                    aa.update_action,
                    action_id,
                    user_guid,
                    next_run_at=next_utc.isoformat(),
                )
                logger.info("Next run for action %d: %s", action_id, job.next_run_time)
        except Exception as e:
            logger.debug("Failed to update next_run_at: %s", e)
