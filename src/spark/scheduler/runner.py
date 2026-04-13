"""Action runner — schedules and executes autonomous actions."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

if TYPE_CHECKING:
    from konfig import AppContext

logger = logging.getLogger(__name__)


class ActionRunner:
    """Loads enabled actions from the database and schedules them via APScheduler."""

    def __init__(self, ctx: Any, daemon_id: str) -> None:
        self._ctx = ctx
        self._daemon_id = daemon_id
        import tzlocal

        self._scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
            timezone=tzlocal.get_localzone(),
        )
        self._poll_interval = 30
        self._poll_thread: threading.Thread | None = None
        self._running = False
        self._db: Any = None
        self._known_action_ids: set[int] = set()

    def _get_db(self) -> Any:
        """Get or create a persistent database connection."""
        if self._db is None:
            from spark.database.backends import create_backend
            from spark.database.connection import DatabaseConnection
            from spark.database.schema import initialise_schema

            backend = create_backend(self._ctx.settings)
            self._db = DatabaseConnection(backend)
            initialise_schema(self._db)
            logger.info("Action runner database connection established")
        return self._db

    def start(self) -> None:
        """Start the scheduler and begin polling for action changes."""
        from spark.core.user_guid import get_user_guid

        uid = get_user_guid(self._ctx)
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
        if self._db:
            self._db.close()
            self._db = None
        logger.info("Action runner stopped")

    def _poll_loop(self) -> None:
        """Poll for action changes."""
        while self._running:
            try:
                self._reload_all()
            except Exception as e:
                logger.error("Poll error: %s", e)
            time.sleep(self._poll_interval)

    def _reload_all(self) -> None:
        """Reload all enabled actions from the database."""
        from spark.database import autonomous_actions

        db = self._get_db()

        try:
            from spark.core.user_guid import get_user_guid

            user_guid = get_user_guid(self._ctx)
            actions = autonomous_actions.get_enabled_actions(db, user_guid)
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
                current_action_ids.add(action_id)
                job_id = f"action_{action_id}"

                if job_id not in scheduled_ids:
                    self._schedule_action(action, job_id, user_guid)
                else:
                    # Log existing scheduled jobs
                    job = self._scheduler.get_job(job_id)
                    if job and job.next_run_time:
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

    def _schedule_action(self, action: dict, job_id: str, user_guid: str = "default") -> None:
        """Schedule a single action."""
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
                    args=[action["id"]],
                    replace_existing=True,
                )

                # Calculate and store next_run_at in UTC for consistent DB storage
                next_fire = trigger.get_next_fire_time(None, datetime.now(local_tz))
                if next_fire:
                    from spark.database import autonomous_actions as aa

                    # Store as UTC in database
                    next_fire_utc = next_fire.astimezone(timezone.utc)
                    aa.update_action(
                        self._get_db(),
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
                    args=[action["id"]],
                    replace_existing=True,
                )

                from spark.database import autonomous_actions as aa

                aa.update_action(self._get_db(), action["id"], user_guid, next_run_at=run_at)

                logger.info("Scheduled one-off action: '%s' at %s", action["name"], run_at)

            else:
                logger.warning(
                    "Unknown schedule_type '%s' for action '%s'", schedule_type, action.get("name")
                )

        except Exception as e:
            logger.error("Failed to schedule action '%s': %s", action.get("name"), e)

    def _execute_action(self, action_id: int) -> None:
        """Execute an action."""
        logger.info("Executing action %d", action_id)
        from spark.scheduler.executor import ActionExecutor

        executor = ActionExecutor(self._ctx, self._daemon_id)
        executor.execute(action_id)

        # Update next_run_at and last_run_at after execution
        try:
            from spark.core.user_guid import get_user_guid
            from spark.database import autonomous_actions as aa

            user_guid = get_user_guid(self._ctx)
            job = self._scheduler.get_job(f"action_{action_id}")
            if job and job.next_run_time:
                # Store as UTC for consistent DB storage
                next_utc = job.next_run_time.astimezone(timezone.utc)
                aa.update_action(
                    self._get_db(),
                    action_id,
                    user_guid,
                    next_run_at=next_utc.isoformat(),
                )
                logger.info("Next run for action %d: %s", action_id, job.next_run_time)
        except Exception as e:
            logger.debug("Failed to update next_run_at: %s", e)
