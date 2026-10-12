"""GitHub OAuth (sign in with GitHub): authorization URL with state + PKCE, code exchange
and identity lookup over httpx (docs/security/AUTH.md §2; BLOCKERS.md B5).

Only GitHub's *verified primary* e-mail is trusted to link or create an account. The HTTP
client is injected (``app.state.http_client``), so tests swap in ``httpx.MockTransport``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL: Final = "https://github.com/login/oauth/authorize"
TOKEN_URL: Final = "https://github.com/login/oauth/access_token"  # noqa: S105 - a URL
API_URL: Final = "https://api.github.com"
SCOPES: Final = "read:user user:email"
_API_HEADERS: Final = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


class GitHubOAuthError(Exception):
    """The exchange or identity lookup failed; ``code`` is safe to show the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GitHubIdentity:
    id: int
    login: str
    name: str | None
    email: str  # verified primary address


def authorize_url(*, client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "allow_signup": "true",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubOAuthError("github_bad_response", "GitHub returned invalid JSON") from exc


async def exchange_code(
    client: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> str:
    try:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        raise GitHubOAuthError("github_unavailable", "could not reach GitHub") from exc
    body = _json(response)
    if response.status_code != 200 or not isinstance(body, dict):
        raise GitHubOAuthError("github_bad_response", f"token endpoint: {response.status_code}")
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        # e.g. {"error": "bad_verification_code"}: expired, reused or forged code.
        raise GitHubOAuthError("github_code_rejected", str(body.get("error", "no token")))
    return token


async def fetch_identity(client: httpx.AsyncClient, access_token: str) -> GitHubIdentity:
    headers = {**_API_HEADERS, "Authorization": f"Bearer {access_token}"}
    try:
        user_resp = await client.get(f"{API_URL}/user", headers=headers)
        emails_resp = await client.get(f"{API_URL}/user/emails", headers=headers)
    except httpx.HTTPError as exc:
        raise GitHubOAuthError("github_unavailable", "could not reach GitHub") from exc
    if user_resp.status_code != 200 or emails_resp.status_code != 200:
        raise GitHubOAuthError("github_bad_response", "GitHub refused the identity lookup")
    user, emails = _json(user_resp), _json(emails_resp)
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise GitHubOAuthError("github_bad_response", "unexpected /user payload")
    if not isinstance(emails, list):
        raise GitHubOAuthError("github_bad_response", "unexpected /user/emails payload")
    primary = next(
        (
            e
            for e in emails
            if isinstance(e, dict)
            and e.get("primary") is True
            and e.get("verified") is True
            and isinstance(e.get("email"), str)
        ),
        None,
    )
    if primary is None:
        raise GitHubOAuthError(
            "github_no_verified_email", "your GitHub account has no verified primary e-mail"
        )
    name = user.get("name")
    return GitHubIdentity(
        id=int(user["id"]),
        login=str(user.get("login", "")),
        name=name if isinstance(name, str) and name else None,
        email=str(primary["email"]),
    )
