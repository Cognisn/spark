"""Entry point for the Spark application.

* ``spark`` or ``spark web`` starts the web interface (in personal mode, with
  the tray daemon when autonomous actions are enabled, as before);
* ``spark worker`` runs the background duties in server mode (scheduled actions,
  one active worker per database);
* ``spark migrate`` brings the database to the current schema (SPARK's
  migration chain, then any registered chain) and exits.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

_USAGE = "usage: spark [web | worker | migrate]"


def main(argv: Sequence[str] | None = None) -> None:
    """Launch the Spark application in a role, or run a subcommand."""
    from spark.core import application

    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["web"]:
        application.run()
        return
    if args == ["worker"]:
        from spark import worker

        raise SystemExit(worker.main())
    if args == ["migrate"]:
        raise SystemExit(application.run_migrate())
    print(f"spark: unknown command {' '.join(args)!r}\n{_USAGE}", file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
