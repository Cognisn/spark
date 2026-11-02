"""warden's mail in SPARK: invitation and password reset links without a mail service.

warden mails invitation and password reset links through a ``MailSender`` the
product supplies, and refuses to invite anyone without one. SPARK has no mail
service of its own in server mode (the SMTP settings belong to the email
tools, which a server does not offer), so :class:`LinkCapture` is the sender:

* inside :func:`captured`, a block an administrator's request opens around
  ``invitations.create``, ``invitations.resend`` or ``resets.request``, each
  message is kept for that block alone, so the screen can show the
  administrator the link once (:func:`link_in`);
* anywhere else (a password reset asked for by someone signed out, warden's
  notice that a second factor was enrolled) the message is dropped. A reset
  request therefore keeps its neutral answer and no link leaves the server.

Nothing here logs a message, a link or a token. A deployment that wants
warden's mails delivered supplies its own ``MailSender`` instead.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
from collections.abc import Iterator

from warden.identity import MailMessage

__all__ = ["LinkCapture", "captured", "link_in"]

logger = logging.getLogger(__name__)

_CAPTURE: contextvars.ContextVar[list[MailMessage] | None] = contextvars.ContextVar(
    "spark_identity_mail_capture", default=None
)


class LinkCapture:
    """A ``MailSender`` that hands messages to the open :func:`captured` block, or drops them."""

    async def send(self, message: MailMessage) -> None:
        held = _CAPTURE.get()
        if held is None:
            logger.info("No mail service: a %s message was not sent", message.kind.value)
            return
        held.append(message)


@contextlib.contextmanager
def captured() -> Iterator[list[MailMessage]]:
    """Keep the messages warden sends during the block (this task only) in the list yielded."""
    held: list[MailMessage] = []
    token = _CAPTURE.set(held)
    try:
        yield held
    finally:
        _CAPTURE.reset(token)


def link_in(messages: list[MailMessage], base_url: str, path: str) -> str | None:
    """The link to ``base_url`` + ``path`` in the last captured message, or None."""
    pattern = re.compile(re.escape(base_url.rstrip("/") + path) + r"\?token=[A-Za-z0-9_\-]+")
    for message in reversed(messages):
        found = pattern.search(message.text)
        if found is not None:
            return found.group(0)
    return None
