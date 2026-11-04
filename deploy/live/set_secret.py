"""Write one secret into SPARK's konfig store from inside a SPARK container (live stack only).

    python /opt/spark-live/set_secret.py NAME [--announce KEY]

NAME is the konfig secret name (for example ``warden.sso.client-secret``, or
``providers_anthropic_api_key`` for the setting ``providers.anthropic.api_key``).
The value is read from standard input (asked for without echo at a terminal)
and is never printed. ``--announce KEY`` then tells every running replica that
the secret behind the setting KEY changed, as the settings page does, so they
reload it without a restart.

It opens konfig exactly as SPARK's worker does, so it writes the same store
(``secrets.backend`` and ``KONFIG_MASTER_KEY`` from the container's environment).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys


def _value() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Secret value (not shown): ")
    return sys.stdin.readline().rstrip("\r\n")


async def _write(name: str, value: str) -> None:
    from konfig import AppContext

    import spark
    from spark.core import application

    config_path = application._get_config_path()
    application._ensure_config(config_path)
    async with AppContext(
        name="Spark live secret",
        version=spark.__version__,
        config_file=str(config_path),
        defaults=application._default_settings(),
        env_prefix="SPARK",
    ) as app:
        app.secrets.set(name, value)
        if app.secrets.get(name) != value:
            raise SystemExit(f"the secrets store did not keep {name}")


def _announce(key: str) -> None:
    import psycopg

    from spark.instance_settings.repository import CHANNEL

    with psycopg.connect(os.environ["SPARK__DATABASE__URL"], autocommit=True) as conn:
        conn.execute("SELECT pg_notify(%s, %s)", (CHANNEL, key))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name")
    parser.add_argument("--announce", metavar="KEY")
    args = parser.parse_args()
    value = _value()
    if not value:
        print("No value given; nothing written.", file=sys.stderr)
        return 2
    asyncio.run(_write(args.name, value))
    if args.announce:
        _announce(args.announce)
    print(f"Stored {args.name}" + (f" and announced {args.announce}" if args.announce else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
