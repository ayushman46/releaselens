"""Notification text: Slack Block Kit payloads and plain-text e-mail.

Wording rules (ARCHITECTURE.md §10): "probable origin", never "root cause"; the severity is a
deterministic 0-100 score, not a probability. Every string that came from a customer
(service names, versions, environments) is escaped for Slack's mrkdwn, so a service named
``<!channel>`` or ``<https://evil|click>`` renders as text instead of pinging a channel or
forging a link.
"""

from __future__ import annotations

from typing import Any

MAX_AFFECTED_LISTED = 10


def slack_escape(text: object) -> str:
    """Slack's three control characters (https://api.slack.com/reference/surfaces/formatting)."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _version(p: dict[str, Any]) -> str:
    prev = p.get("previous_version")
    return f"{p.get('version')} (from {prev})" if prev else str(p.get("version"))


def headline(kind: str, p: dict[str, Any]) -> str:
    if kind == "risk":
        score = p.get("risk_score")
        scored = f" (risk score {score}/100)" if score is not None else ""
        return f"Pre-deploy risk {p.get('band')}{scored} for {p.get('service') or 'a change'}"
    score = p.get("severity_score")
    scored = f" (severity {score:.1f}/100)" if isinstance(score, int | float) else ""
    return (
        f"Regression {p.get('severity_band')}{scored} after deploying {p.get('service')} "
        f"{_version(p)} to {p.get('environment')}"
    )


def detail_lines(kind: str, p: dict[str, Any]) -> list[str]:
    if kind == "risk":
        lines = [f"Band: {p.get('band')}"]
        if p.get("repository"):
            lines.append(f"Repository: {p.get('repository')}")
        if p.get("commit_sha"):
            lines.append(f"Commit: {p.get('commit_sha')}")
        return lines
    origin = p.get("probable_origin")
    confidence = p.get("confidence")
    lines = [f"Status of {p.get('service')}: {p.get('status')}"]
    lines.append(
        f"Probable origin: {origin} (confidence {confidence})"
        if origin
        else "Probable origin: not determined"
        + (f" (confidence {confidence})" if confidence else "")
    )
    affected = list(p.get("affected_services") or [])
    if affected:
        shown = ", ".join(str(a) for a in affected[:MAX_AFFECTED_LISTED])
        more = len(affected) - MAX_AFFECTED_LISTED
        lines.append(f"Affected services: {shown}" + (f" and {more} more" if more > 0 else ""))
    lines.append(f"Analysis: {p.get('kind')} ({p.get('engine_version')})")
    return lines


def link_for(kind: str, p: dict[str, Any], web_base_url: str) -> str:
    base = web_base_url.rstrip("/")
    if kind == "risk":
        return f"{base}/app/risk/{p.get('risk_assessment_id')}"
    return f"{base}/app/deployments/{p.get('deployment_id')}"


def slack_message(kind: str, p: dict[str, Any], web_base_url: str) -> dict[str, Any]:
    title = slack_escape(headline(kind, p))
    details = "\n".join(slack_escape(line) for line in detail_lines(kind, p))
    link = link_for(kind, p, web_base_url)
    return {
        "text": title,  # notification / fallback text
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n{details}"}},
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"<{link}|Open in ReleaseLens>"}],
            },
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def email_message(kind: str, p: dict[str, Any], web_base_url: str) -> tuple[str, str]:
    subject = f"[ReleaseLens] {headline(kind, p)}"
    # Header values must stay on one line (the e-mail library refuses CR/LF anyway).
    subject = " ".join(subject.split())[:200]
    body = "\n".join(
        [
            headline(kind, p),
            "",
            *detail_lines(kind, p),
            "",
            f"Details: {link_for(kind, p, web_base_url)}",
            "",
            "You receive this because an alert rule in ReleaseLens lists this address.",
        ]
    )
    return subject, body + "\n"
