"""``spark bootstrap [--resend-invitation]``: run the server's first-run steps and exit.

Reads the configuration as ``spark web`` does, refuses personal mode and a database
behind the schema (``spark migrate`` first), then runs :func:`spark.bootstrap.run.run`.
Exit codes: 0 done, 2 refused (with every problem on standard error), 1 an
unexpected failure. The output names what was done, never an address, a URL, a
password or an invitation link.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from spark.bootstrap.run import Outcome

__all__ = ["describe", "main", "parse"]

PROG = "spark bootstrap"


def parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Create the restricted database logins, the instance identity and the first "
            "administrator's invitation of a Spark server (safe to run again)."
        ),
    )
    parser.add_argument(
        "--resend-invitation",
        action="store_true",
        help="resend the first administrator's invitation (it must not yet be accepted)",
    )
    return parser.parse_args(list(argv))


_BEDROCK = {
    "created": "bedrock connection: aws-bedrock added (task role credentials)",
    "exists": "bedrock connection: aws-bedrock already exists, left unchanged",
    "declined": "bedrock connection: aws-bedrock was deleted by an administrator, not added again",
}


def describe(outcome: Outcome) -> str:
    """What happened, one line per step."""
    lines: list[str] = []
    if outcome.logins:
        lines.append(
            "database logins: "
            + ", ".join(f"{name} {change}" for name, change in outcome.logins.items())
        )
    else:
        lines.append("database logins: not configured (every role connects as the owner)")
    lines.append("instance identity: present")
    if outcome.mail_seeded:
        lines.append("mail settings: stored from the start-up settings")
    if outcome.bedrock:
        lines.append(_BEDROCK[outcome.bedrock])
    if outcome.invited:
        lines.append("first administrator: invitation sent")
    elif outcome.already_complete:
        lines.append("first administrator: bootstrap already complete, nothing sent")
    else:
        lines.append("first administrator: an administrator already exists, nothing sent")
    if outcome.resent:
        lines.append("first administrator: invitation resent")
    lines.append("bootstrap: complete")
    return "\n".join(lines)


def main(argv: Sequence[str]) -> int:
    """Run ``spark bootstrap``; return the exit code."""
    from konfig import AppContext

    import spark
    from spark import mode
    from spark.core import application
    from spark.db import migrate
    from spark.db.migrate import MigrationError
    from spark.identity import migrations as identity_migrations

    options = parse(argv)
    config_path = application._get_config_path()
    application._ensure_config(config_path)

    async def _run() -> Outcome:
        from spark.bootstrap.run import run

        async with AppContext(
            name="Spark",
            version=spark.__version__,
            config_file=str(config_path),
            defaults=application._default_settings(),
            env_prefix="SPARK",
        ) as ctx:
            runtime = application.validate_startup(ctx, config_path)
            if runtime.mode is not mode.Mode.SERVER:
                raise mode.ModeConfigError(
                    "spark bootstrap runs only in server mode: personal mode has one local user"
                )
            identity_migrations.register()
            await asyncio.to_thread(migrate.check_at_head, runtime.database)
            return await run(
                runtime.database,
                ctx.settings,
                ctx.secrets,
                resend_invitation=options.resend_invitation,
            )

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        outcome = asyncio.run(_run())
    except (mode.ModeConfigError, MigrationError) as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    print(describe(outcome))
    return 0
