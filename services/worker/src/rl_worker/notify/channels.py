"""Delivery channels: Slack incoming webhooks (httpx) and SMTP e-mail.

Each failure is classified for the dispatcher:

* **transient** (retry with backoff): timeouts, connection errors, HTTP 429 (honouring
  ``Retry-After``) and 5xx, SMTP 4xx and dropped connections;
* **permanent** (dead immediately): other HTTP 4xx (Slack answers ``invalid_token``,
  ``no_service``, ``channel_is_archived`` …), redirects (never followed: a webhook that
  redirects is not a Slack webhook), SMTP 5xx and refused recipients.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from collections.abc import Sequence
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Protocol

import httpx


class DeliveryError(Exception):
    def __init__(
        self, message: str, *, permanent: bool, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.retry_after_s = retry_after_s


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None


class SlackSender:
    """POST a Block Kit payload. The client must have a timeout and no redirect following."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def send(self, url: str, payload: dict[str, Any]) -> None:
        try:
            response = await self._client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise DeliveryError("slack webhook timed out", permanent=False) from exc
        except httpx.HTTPError as exc:
            raise DeliveryError(
                f"slack webhook unreachable: {type(exc).__name__}", permanent=False
            ) from exc
        status = response.status_code
        if 200 <= status < 300:
            return
        reason = response.text[:200].strip() or response.reason_phrase
        if status == 429:
            raise DeliveryError(
                "slack rate limited the webhook",
                permanent=False,
                retry_after_s=_retry_after(response),
            )
        if status >= 500:
            raise DeliveryError(f"slack answered {status}: {reason}", permanent=False)
        if 300 <= status < 400:
            raise DeliveryError(
                f"slack webhook redirected ({status}); not followed", permanent=True
            )
        raise DeliveryError(f"slack refused the webhook ({status}): {reason}", permanent=True)


class EmailSender(Protocol):
    async def send(self, to: Sequence[str], subject: str, text: str) -> None: ...


class SmtpEmailSender:
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

    def _send_sync(self, to: Sequence[str], subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["From"] = self._sender
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain="releaselens.local")
        msg.set_content(text)
        with smtplib.SMTP(self._host, self._port, timeout=self._timeout_s) as smtp:
            if self._starttls:
                smtp.starttls(context=ssl.create_default_context())
            if self._username and self._password:
                smtp.login(self._username, self._password)
            smtp.send_message(msg)

    async def send(self, to: Sequence[str], subject: str, text: str) -> None:
        try:
            await asyncio.to_thread(self._send_sync, to, subject, text)
        except smtplib.SMTPRecipientsRefused as exc:
            raise DeliveryError("every recipient was refused", permanent=True) from exc
        except smtplib.SMTPResponseException as exc:
            permanent = 500 <= exc.smtp_code < 600
            raise DeliveryError(f"smtp answered {exc.smtp_code}", permanent=permanent) from exc
        except (
            smtplib.SMTPException,
            OSError,
        ) as exc:  # disconnects, refused connections, timeouts
            raise DeliveryError(f"smtp unavailable: {type(exc).__name__}", permanent=False) from exc


@dataclass
class RecordingEmailSender:
    """Test double."""

    sent: list[tuple[tuple[str, ...], str, str]] = field(default_factory=list)
    fail_with: DeliveryError | None = None

    async def send(self, to: Sequence[str], subject: str, text: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((tuple(to), subject, text))
