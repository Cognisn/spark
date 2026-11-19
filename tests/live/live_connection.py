"""Add the connection the one live chat uses, with a placeholder key (live suite only).

    python -m tests.live.live_connection --service anthropic [--id live]

Signs in to the stack named by ``SPARK_LIVE_STACK`` as its first administrator,
adds the connection through the connections API (the modal's), with the
placeholder key ``placeholder-dev-only`` so the connection is valid, and assigns it
to the administrator role. It then prints the command Matthew runs in his own
terminal to replace the placeholder with the real key, which goes straight into
the stack's konfig store and is never seen here. Services: ``anthropic``,
``gemini``, ``openai`` and ``xai`` (each takes an API key at ``api_key`` or
``service.api_key``).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

from tests.live.client import Browser
from tests.live.conftest import STATE_ENV, Stack

KEY_FIELDS = {
    "anthropic": "api_key",
    "gemini": "api_key",
    "openai": "service.api_key",
    "xai": "service.api_key",
}
PLACEHOLDER = "placeholder-dev-only"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--service", choices=sorted(KEY_FIELDS), required=True)
    parser.add_argument("--id", default="live")
    args = parser.parse_args(argv)

    stack = Stack(json.loads(Path(os.environ[STATE_ENV]).read_text("utf-8")))
    email = stack.state["admin_email"]
    known = stack.remembered().get(email, {})
    if not known:
        raise SystemExit(
            "the first administrator has not accepted bootstrap's invitation yet: run the "
            "live suite (tests/live/test_rc1.py) once first"
        )
    secret = base64.b32decode(known["totp"]) if known.get("totp") else None
    browser = Browser(stack.base_url)
    browser.sign_in(email, known["password"], secret)
    field = KEY_FIELDS[args.service]
    if browser.get(f"/api/connections/{args.id}").status_code == 404:
        made = browser.post(
            "/api/connections",
            json={
                "id": args.id,
                "service": args.service,
                "name": f"Live {args.service}",
                "values": {},
                "secrets": {field: PLACEHOLDER},
            },
        )
        if made.status_code != 201:
            raise SystemExit(f"the connection was refused: {made.text}")
        assigned = browser.put(
            f"/api/connections/{args.id}/assignments",
            json={"role": "administrator", "no_limit": True},
        )
        if assigned.status_code != 200:
            raise SystemExit(f"the assignment was refused: {assigned.text}")
    browser.close()
    name = f"connections.{args.id}.{field}"
    print(f"Connection {args.id!r} ({args.service}) is ready. Store the real key with:")
    print(
        f"  python deploy/live/stack.py --sandbox {stack.sandbox} secret {name} --announce {name}"
    )
    print(f"then run the test with SPARK_LIVE_CONNECTION={args.id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
