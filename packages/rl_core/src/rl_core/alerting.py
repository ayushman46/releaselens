"""Alert-channel rules shared by the API (validation on save) and the worker (validation
again before every send): what counts as an acceptable Slack incoming-webhook URL.

A webhook URL is a credential *and* a request target. Accepting arbitrary URLs would turn
the notifier into a server-side request forgery tool (``http://169.254.169.254/…``,
internal services), so outside dev/test only ``https://hooks.slack.com/services/…`` is
allowed. Dev and test may point at a local mock receiver.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

SLACK_HOST: Final = "hooks.slack.com"
MAX_WEBHOOK_URL_LEN: Final = 512


def validate_slack_webhook_url(url: str, *, allow_any_host: bool) -> str:
    """Return ``url`` stripped, or raise ``ValueError`` saying what is wrong."""
    url = url.strip()
    if not url or len(url) > MAX_WEBHOOK_URL_LEN:
        raise ValueError(f"webhook URL must be 1..{MAX_WEBHOOK_URL_LEN} characters")
    if any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ValueError("webhook URL must not contain spaces or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("webhook URL is malformed") from exc
    if parts.username or parts.password:
        raise ValueError("webhook URL must not contain credentials")
    if allow_any_host:
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("webhook URL must be an http(s) URL")
        return url
    if parts.scheme != "https" or parts.hostname != SLACK_HOST or port not in (None, 443):
        raise ValueError(f"webhook URL must be https://{SLACK_HOST}/services/…")
    if not parts.path.startswith("/services/"):
        raise ValueError(f"webhook URL must be https://{SLACK_HOST}/services/…")
    return url


def webhook_hint(url: str) -> str:
    """What may be shown of a stored webhook: scheme, host and the last 4 characters."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}/…{url[-4:]}"
