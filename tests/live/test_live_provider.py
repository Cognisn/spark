"""A18 item 6 and scope 7: one live chat through a connection to the service Matthew names.

Skipped unless ``SPARK_LIVE_CONNECTION`` names a connection on the stack whose key
Matthew stored himself. The steps (the runbook's "Live provider"):

1. ``python -m tests.live.live_connection --service anthropic`` (or ``gemini``,
   ``openai``, ``xai``) adds the connection ``live`` through the connections API
   with a placeholder key, assigned to the administrator role, and prints the
   command for step 2.
2. Matthew runs ``stack.py secret connections.live.api_key --announce
   connections.live.api_key`` in his own terminal; the key goes straight into the
   stack's konfig store, never echoed, and every replica rebuilds the connection.
3. ``SPARK_LIVE_CONNECTION=live`` (``SPARK_LIVE_MODEL`` optional, a model id the
   connection offers) and this test: discovery reaches the service, one turn
   returns a reply and the call is recorded in ``usage_tracking`` with tokens.

The suite never sees, sends or prints the key.
"""

from __future__ import annotations

import os

import pytest

from tests.live.client import final_text, named
from tests.live.conftest import Person, Stack, offered, wait_for

CONNECTION = os.environ.get("SPARK_LIVE_CONNECTION", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(300),
    pytest.mark.skipif(not CONNECTION, reason="SPARK_LIVE_CONNECTION names no live connection"),
]


def test_one_live_chat_through_the_named_connection(stack: Stack, admin: Person) -> None:
    """A18 item 6: a chat turn through the real service returns a reply and records usage."""
    shown = admin.browser.get(f"/api/connections/{CONNECTION}")
    assert shown.status_code == 200, f"no connection {CONNECTION!r}: {shown.text}"
    assert any(shown.json()["secrets"].values()), "no key stored for the connection"

    view = admin.browser.post(f"/api/connections/{CONNECTION}/discover").json()
    discovery = view["discovery"] or {}
    assert discovery.get("ok") or discovery.get("supported") is False, discovery

    wanted = os.environ.get("SPARK_LIVE_MODEL")

    def model() -> str | None:
        ours = [m["model_id"] for m in offered(admin) if m.get("connection_id") == CONNECTION]
        if wanted:
            return wanted if wanted in ours else None
        return ours[0] if ours else None

    model_id: str = wait_for(model, 60, f"a model offered on {CONNECTION}")

    def calls() -> tuple[int, int]:
        row = stack.sql(
            "SELECT count(*), COALESCE(SUM(input_tokens + output_tokens), 0)"
            " FROM usage_tracking WHERE connection_id = %s",
            (CONNECTION,),
        )[0]
        return int(row[0]), int(row[1])

    before = calls()
    conv = admin.browser.new_conversation(f"{CONNECTION}/{model_id}", "Live provider")
    events = admin.browser.chat(conv, "Reply with the single word READY and nothing else.")
    assert not named(events, "error"), [e.data for e in named(events, "error")]
    assert named(events, "complete")[-1].data.get("status") != "refused"
    reply = final_text(events)
    assert reply.strip(), [e.name for e in events]
    assert "READY" in reply.upper(), reply

    after = wait_for(lambda: calls() if calls()[0] > before[0] else None, 30, "usage recorded")
    assert after[1] > before[1], (before, after)
