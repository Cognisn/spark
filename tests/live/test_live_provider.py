"""A18 item 6 (and wave 1 Q5): one live chat against the provider Matthew names.

Skipped unless ``SPARK_LIVE_PROVIDER`` names the provider's settings section
(``anthropic``, ``google_gemini``, ``xai`` or ``aws_bedrock``), on a stack
started with ``stack.py up --live`` (so no request is sent to the model stub).
Matthew enters the key himself into the stack's konfig store with
``deploy/live/set_secret.py``; the suite never sees, sends or prints it.
``SPARK_LIVE_MODEL`` may name the model; otherwise the provider's first listed
model is used.
"""

from __future__ import annotations

import os

import pytest

from tests.live.client import final_text, named
from tests.live.conftest import Person, Stack, wait_for

PROVIDER = os.environ.get("SPARK_LIVE_PROVIDER", "")
NAMES = {
    "anthropic": "Anthropic",
    "google_gemini": "Google Gemini",
    "xai": "X.AI",
    "aws_bedrock": "AWS Bedrock",
}

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(300),
    pytest.mark.skipif(not PROVIDER, reason="SPARK_LIVE_PROVIDER names no live provider"),
]


def test_one_live_chat_against_the_named_provider(stack: Stack, admin: Person) -> None:
    """A18 item 6: a chat turn through the real provider returns a reply and records usage."""
    assert PROVIDER in NAMES, f"unknown provider {PROVIDER!r}; one of {sorted(NAMES)}"
    env = stack.docker("exec", stack.container("web"), "printenv")
    assert "ANTHROPIC_BASE_URL" not in env, "this stack sends Anthropic calls to the stub"

    secrets = admin.browser.get("/settings/api/secrets").json()
    key_setting = f"providers.{PROVIDER}.api_key"
    if PROVIDER != "aws_bedrock":
        assert secrets.get(key_setting), f"no key stored for {key_setting}"
    saved = admin.browser.post(
        "/settings/api/instance", json={f"providers.{PROVIDER}.enabled": True}
    )
    assert saved.status_code == 200, saved.text

    wanted = os.environ.get("SPARK_LIVE_MODEL")

    def model() -> str | None:
        models = admin.browser.get("/conversations/api/models").json()["models"]
        ours = [m["id"] for m in models if m.get("provider") == NAMES[PROVIDER]]
        if wanted:
            return wanted if wanted in ours else None
        return ours[0] if ours else None

    model_id: str = wait_for(model, 60, f"a {NAMES[PROVIDER]} model listed")
    before = admin.browser.get("/api/usage/me").json()["used"]

    conv = admin.browser.new_conversation(model_id, "Live provider")
    events = admin.browser.chat(conv, "Reply with the single word READY and nothing else.")
    assert not named(events, "error"), [e.data for e in named(events, "error")]
    assert named(events, "complete")[-1].data.get("status") != "refused"
    reply = final_text(events)
    assert reply.strip(), [e.name for e in events]
    assert "READY" in reply.upper(), reply

    after = admin.browser.get("/api/usage/me").json()["used"]
    assert after > before, (before, after)
