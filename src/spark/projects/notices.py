"""What a person is told about a project by mail (server mode, when a sender is set).

In SPARK, notices are rows of ``project_notices``
(:func:`spark.database.projects.add_notice`), shown until seen. By mail, through the
instance's ``mail.smtp`` sender, three notices go out:

* ``shared``: a project was shared with the person (their role and scope);
* ``transferred``: they were made the owner of a project (PC14);
* ``deleted``: a project was deleted with conversations they had started in it (PC13).

A notice names the person who acted, the project and how many conversations, never
anything said in them, and never raises: a notice that cannot be delivered is logged
(without the address) and what was done stands.
"""

from __future__ import annotations

import asyncio
import logging
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Final

from spark.identity.mail import SecretReader, SmtpSender, SmtpSettings, smtp_settings

__all__ = ["KINDS", "build_project_notice", "send_project_notice"]

logger = logging.getLogger(__name__)

KINDS: Final = ("shared", "transferred", "deleted")


def _clean(text: str, limit: int = 200) -> str:
    return " ".join(str(text).split())[:limit]


def build_project_notice(
    smtp: SmtpSettings,
    to: str,
    *,
    kind: str,
    actor: str,
    project: str,
    detail: str = "",
    count: int = 0,
    link: str = "",
) -> EmailMessage:
    """The notice ``kind`` for ``to``: ``detail`` is the role and scope of a share."""
    if kind not in KINDS:
        raise ValueError(f"unknown notice: {kind}")
    who, name = _clean(actor), _clean(project)
    if kind == "shared":
        subject = f"{who} shared a project with you"
        lead = f'{who} shared the project "{name}" with you in Spark, {_clean(detail)}.'
    elif kind == "transferred":
        subject = f"{who} made you the owner of a project"
        lead = (
            f'{who} made you the owner of the project "{name}" in Spark. Its resources now '
            "count against your storage."
        )
    else:
        noun = "conversation" if count == 1 else "conversations"
        subject = f"{who} deleted a project with your conversations"
        lead = (
            f'{who} deleted the project "{name}" in Spark, and with it {count} {noun} you '
            "had started there."
        )
    email = EmailMessage()
    email["From"] = formataddr((smtp.sender_name, smtp.sender_address))
    email["To"] = to
    email["Subject"] = subject
    email["Date"] = formatdate(usegmt=True)
    email["Message-ID"] = make_msgid(domain=smtp.sender_address.rpartition("@")[2] or "localhost")
    email.set_content(f"{lead}\n\n" + (f"Open Spark: {link}\n" if link else ""))
    return email


async def send_project_notice(
    secrets: SecretReader | None,
    to: str,
    *,
    kind: str,
    actor: str,
    project: str,
    detail: str = "",
    count: int = 0,
    link: str = "",
) -> bool:
    """Mail a project notice when a mail sender is configured; returns whether it was sent."""
    try:
        smtp = await asyncio.to_thread(smtp_settings, secrets)
        if smtp is None:
            return False
        email = build_project_notice(
            smtp,
            to,
            kind=kind,
            actor=actor,
            project=project,
            detail=detail,
            count=count,
            link=link,
        )
        await SmtpSender(smtp).deliver(email, f"project {kind}")
    except Exception:  # noqa: BLE001 - a notice never undoes what was done
        logger.warning("A project %s notice was not delivered", kind)
        return False
    return True
