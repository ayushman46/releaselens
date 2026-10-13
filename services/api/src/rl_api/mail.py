"""Outgoing e-mail: a small :class:`Mailer` interface, an SMTP implementation (Mailpit in
dev, ``RL_SMTP_*``) and a recording fake for tests.

Mail is sent from FastAPI background tasks *after* the response, so SMTP latency or an
outage never changes a response (or its timing, which would leak whether an account
exists). A failure is logged and swallowed; the user can ask for the e-mail again.

Tokens travel in the URL **fragment** (``#token=…``): browsers never send fragments to a
server, so the token stays out of web-server logs and ``Referer`` headers.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Protocol

log = logging.getLogger("rl_api.mail")


@dataclass(frozen=True, slots=True)
class Email:
    to: tuple[str, ...]
    subject: str
    text: str
    tags: tuple[str, ...] = field(default=())  # for tests and logs, e.g. ("verify_email",)


class Mailer(Protocol):
    async def send(self, email: Email) -> None: ...


class SmtpMailer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = False,
        timeout_s: float = 10.0,
    ) -> None:
        self._host, self._port, self._sender = host, port, sender
        self._username, self._password = username, password
        self._starttls, self._timeout_s = starttls, timeout_s

    def _build(self, email: Email) -> EmailMessage:
        msg = EmailMessage()  # the default policy refuses CR/LF in header values
        msg["From"] = self._sender
        msg["To"] = ", ".join(email.to)
        msg["Subject"] = email.subject
        msg["Message-ID"] = make_msgid(domain="releaselens.local")
        msg.set_content(email.text)
        return msg

    def _send_sync(self, email: Email) -> None:
        msg = self._build(email)
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout_s) as smtp:
            if self._starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(msg)

    async def send(self, email: Email) -> None:
        await asyncio.to_thread(self._send_sync, email)


class RecordingMailer:
    """Test double: keeps every e-mail instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[Email] = []

    async def send(self, email: Email) -> None:
        self.sent.append(email)

    def last(self, tag: str, to: str | None = None) -> Email:
        for email in reversed(self.sent):
            if tag in email.tags and (to is None or to in email.to):
                return email
        raise LookupError(f"no {tag!r} e-mail recorded" + (f" for {to}" if to else ""))


async def send_quietly(mailer: Mailer, email: Email) -> None:
    """Background-task entry point: never raises."""
    try:
        await mailer.send(email)
    except Exception as exc:
        log.warning(
            "e-mail not sent",
            extra={"tags": list(email.tags), "error": type(exc).__name__},
        )


# ----------------------------------------------------------------------------- templates


def verification_email(to: str, link: str) -> Email:
    return Email(
        to=(to,),
        subject="Confirm your ReleaseLens e-mail address",
        text=(
            "Confirm this address to finish creating your ReleaseLens account:\n\n"
            f"{link}\n\n"
            "The link works once and expires in 24 hours. If you did not sign up, ignore "
            "this message and no account will be activated.\n"
        ),
        tags=("verify_email",),
    )


def account_exists_email(to: str, reset_link: str) -> Email:
    return Email(
        to=(to,),
        subject="Your ReleaseLens account",
        text=(
            "Someone tried to create a ReleaseLens account with this address, but one "
            "already exists.\n\n"
            "If it was you, sign in, or choose a new password here (the link expires in "
            f"1 hour):\n\n{reset_link}\n\n"
            "If it was not you, you can ignore this message.\n"
        ),
        tags=("account_exists",),
    )


def password_reset_email(to: str, link: str) -> Email:
    return Email(
        to=(to,),
        subject="Reset your ReleaseLens password",
        text=(
            "Choose a new ReleaseLens password here:\n\n"
            f"{link}\n\n"
            "The link works once and expires in 1 hour. Every signed-in session is signed "
            "out when the password changes. If you did not ask for this, ignore it.\n"
        ),
        tags=("reset_password",),
    )


def invitation_email(to: str, *, org_name: str, inviter: str, role: str, link: str) -> Email:
    return Email(
        to=(to,),
        subject=f"You are invited to {org_name} on ReleaseLens",
        text=(
            f"{inviter} invited you to join {org_name} on ReleaseLens as {role}.\n\n"
            f"Accept the invitation (sign in or create an account with this address):\n\n"
            f"{link}\n\nThe invitation expires in 7 days.\n"
        ),
        tags=("invitation",),
    )
