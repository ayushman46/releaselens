# Authentication, authorization and audit (M10)

Status: implemented in `services/api` (routers under `/api/v1`), `packages/rl_db` (migration
`0004_saas`) and `services/worker` (notifier). The three design decisions marked
**Proposed ADR** at the end are written for `docs/DECISIONS.md` once reviewed.

## 1. Threat model (summary)

| Asset | Threat | Control |
|---|---|---|
| Tenant data (deployments, telemetry, alerts, keys) | Another tenant reads or changes it | The tenant comes only from an API key row or a verified membership, never from client input; repositories require a `TenantContext`; composite foreign keys keep children in their parent's tenant; foreign ids answer **404** (§4). Tested for every org route (`test_tenant_matrix.py`). |
| User accounts | Credential stuffing, password guessing | Argon2id, password policy, per-IP and per-(account, IP) token buckets, identical failure responses, failure audit entries |
| User accounts | Account enumeration | Signup, resend-verification and reset requests answer the same `202` and do the same Argon2 work either way; e-mail is sent after the response |
| User accounts | Pre-hijacking (attacker registers the victim's address first) | Signup never modifies an existing account; login requires a verified address; GitHub sign-in on an unverified account drops the squatter's password; password reset proves address control and marks it verified |
| Browser sessions | Token theft (XSS, logs, Referer) | Opaque tokens in `HttpOnly` cookies; only hashes stored; e-mailed tokens travel in the URL fragment; `Cache-Control: no-store` on auth responses |
| Browser sessions | Refresh-token replay | Rotation on every refresh; reuse of a rotated token revokes the whole session (family) |
| Browser sessions | CSRF | `SameSite=Lax`, JSON-only bodies, plus a session-bound double-submit token on every cookie-authenticated mutation |
| Integration secrets (Slack webhook URLs) | DB dump, SSRF via the notifier | AES-256-GCM at rest bound to the row; host allow-list (`hooks.slack.com`) on save and again before every send; no redirects; timeouts |
| Audit trail | Tampering | Insert-only table; `UPDATE`/`DELETE`/`TRUNCATE` raise in a trigger |
| Billing | Forged webhooks, replays | Stripe signature (HMAC-SHA256, 5-minute tolerance) via the SDK; idempotency by event id; ordering guard on event time |
| Availability | Floods on auth / ingest, oversized bodies | Postgres token buckets with `429` + `Retry-After`; 1 MiB request body limit (`413`) |

Out of scope for M10 (open issues, §10): Postgres row-level security (ADR-0010 item 3),
MFA/WebAuthn, CAPTCHA, account lockout notifications, per-user session management UI.

## 2. Accounts

* **Signup** `POST /api/v1/auth/signup {email, password, display_name?}` → `202`. New
  address: user created unverified, verification e-mail (token `rlv_…`, 24 h, single use).
  Existing address: nothing changes; the owner gets an "account exists" e-mail with a reset
  link.
* **Verify** `POST /api/v1/auth/verify-email {token}`; **resend**
  `POST /api/v1/auth/verify-email/resend {email}` (`202`, constant).
* **Password policy** (`rl_api.passwords`, no network): 12–256 characters, no control
  characters, not a listed common password nor a common word decorated with digits or
  symbols (`Password123!`, `P@ssw0rd2024`), more than 3 distinct characters, not a keyboard or
  alphabet run, not containing the product name or the e-mail's local part. Violations
  answer `422 weak_password` with `details.reasons`.
* **Hashing**: Argon2id (`argon2-cffi`), defaults t=3, m=64 MiB, p=4 (RFC 9106 §4 second
  recommendation), `RL_ARGON2_TIME_COST|MEMORY_KIB|PARALLELISM`; hashing runs in a thread,
  at most `RL_PASSWORD_HASH_CONCURRENCY` (2) at once. When the parameters change, the next
  successful login re-hashes. Unknown accounts verify against a dummy hash (equal timing).
* **Password reset** `POST …/password-reset/request {email}` (`202`, constant) and
  `POST …/password-reset/confirm {token, new_password}`: token `rlp_…`, 1 h, single use;
  a weak new password rolls back (the token stays usable); success sets the password,
  marks the address verified, retires other reset tokens and **revokes every session**.
* **GitHub OAuth** (`RL_GITHUB_OAUTH_CLIENT_ID/SECRET`, optional
  `RL_GITHUB_OAUTH_REDIRECT_URI`; unset → `503 github_oauth_not_configured`):
  `GET /api/v1/auth/github/authorize` stores `sha256(state)` with a PKCE S256 verifier
  (10 min) and sets an `HttpOnly` `rl_oauth_state` cookie, then redirects to GitHub.
  `GET /api/v1/auth/github/callback` requires the state to match the cookie (a callback
  link started in another browser fails), consumes the state, exchanges the code with the
  verifier, reads `/user` and `/user/emails`, and accepts only the **verified primary**
  address. Linking: existing GitHub id → that user; else existing user with that address →
  linked (an *unverified* one loses its password first); else a new verified user without a
  password. Errors redirect to `{RL_WEB_BASE_URL}/login?error=<code>`.

## 3. Sessions and CSRF

| Cookie | Content | Lifetime | Flags |
|---|---|---|---|
| `rl_access` | opaque `rla_…` token | 15 min (`RL_ACCESS_TOKEN_TTL_S`) | `HttpOnly; Secure; SameSite=Lax; Path=/` |
| `rl_refresh` | opaque `rlr_…` token | 14 days (`RL_REFRESH_TOKEN_TTL_S`), never past the session's 30-day absolute limit (`RL_SESSION_MAX_AGE_S`) | `HttpOnly; Secure; SameSite=Lax; Path=/api/v1/auth` |
| `rl_csrf` | session CSRF token | as `rl_refresh` | `Secure; SameSite=Lax; Path=/` (readable by JS) |

`Secure` is dropped only when `RL_ENV=dev` and the request is plain `http://localhost` or
`127.0.0.1`. `RL_COOKIE_DOMAIN` sets a parent domain when the web app and API live on
sibling hosts.

* `auth_sessions` holds one row per login (the refresh-token *family*): the current access
  token's hash and expiry, the absolute expiry, revocation time and reason, IP, user agent.
  `auth_refresh_tokens` holds each link of the chain with `used_at`.
* **Refresh** `POST /api/v1/auth/refresh` (refresh cookie + CSRF): a token already rotated
  → the session is revoked (`refresh_token_reuse`), cookies are cleared,
  `401 refresh_token_reused`. Otherwise the token is claimed atomically
  (`UPDATE … WHERE used_at IS NULL`; losing that race is also treated as reuse), a new
  access token replaces the old one (which stops working at once) and the next refresh
  token is issued. The CSRF check runs *before* the claim, so a failed check never burns
  the user's token.
* **Logout** `POST /api/v1/auth/logout` revokes the session found by the access *or*
  refresh cookie (so it works with an expired access token), clears the cookies, `204`.
* **Front-end contract**: on `401` call refresh once, then retry; serialise refreshes
  across tabs (e.g. `navigator.locks`), because two tabs refreshing with the same token is
  indistinguishable from theft and ends the session.
* **CSRF**: every cookie-authenticated `POST/PUT/PATCH/DELETE` needs `X-CSRF-Token` equal to
  the `rl_csrf` cookie **and** to `base64url(HMAC-SHA256(K_csrf, "csrf:" ‖ session_id))`,
  where `K_csrf = HKDF(RL_SECRET_KEY, "releaselens/csrf/v1")`. Binding to the session defeats
  cookie tossing from a sibling subdomain; deriving it means nothing extra is stored and it
  survives refreshes. Login and signup carry no session; they are protected by `SameSite`
  and by FastAPI's strict JSON content type (a cross-site form cannot send JSON). API-key
  requests are exempt (browsers never attach keys on their own).

## 4. The unified principal and tenant selection

`rl_api.principal.get_caller` yields **either** an API key's `TenantContext` (when
`Authorization: Bearer` or `x-releaselens-key` is present; a bad key is `401` even with a
valid cookie) **or** a session user. `require_org(min_role, api_key_scope=…)` admits a
caller to `/api/v1/orgs/{org_id}/…`:

1. API key: `key.tenant_id ≠ org_id` → **404**; route not open to keys → `403
   session_required`; key lacks the scope → `403 insufficient_scope`.
2. Session: no membership in `org_id` → **404**; role below the minimum → `403 forbidden`.

Tenant-implicit routes (`GET /api/v1/deployments[/{id}]`) take a `read` key, or a session
plus `X-RL-Org: <org id>` validated the same way (missing → `400 org_required`).
`POST /api/v1/deployments` stays API-key only.

## 5. RBAC matrix

Roles: **owner > admin > member > viewer**. "key" = an API key of the org with the `read`
scope (keys never mutate org resources; ingest uses `deployments:write` etc.).

| Area | viewer | member | admin | owner | key (`read`) |
|---|---|---|---|---|---|
| Read org, projects, services, environments, alert rules (webhook masked) | ✓ | ✓ | ✓ | ✓ | ✓ |
| Read members, notification log, billing status | ✓ | ✓ | ✓ | ✓ | – |
| Create/edit projects; create/edit/delete services and environments | – | ✓ | ✓ | ✓ | – |
| Delete a project (cascades) | – | – | ✓ | ✓ | – |
| Rename org (settings) | – | – | ✓ | ✓ | – |
| Invite, list/withdraw invitations, change roles, remove members | – | – | ✓¹ | ✓ | – |
| API keys: create (secret shown once), list, revoke, rotate | – | – | ✓ | ✓ | – |
| Alert rules and integrations (Slack webhook, e-mail recipients) | – | – | ✓ | ✓ | – |
| Audit log and CSV export | – | – | ✓ | ✓ | – |
| Billing: Checkout, Customer Portal | – | – | – | ✓ | – |
| Delete the organization | – | – | – | ✓² | – |
| Leave the organization (remove yourself) | ✓ | ✓ | ✓ | ✓³ | – |

¹ Only an owner grants, changes or removes the **owner** role (including inviting an owner).
² Refused (`409 subscription_active`) while a Stripe subscription is live.
³ The **last owner** can be neither demoted nor removed (`409 last_owner`).

Members create projects because they are the engineers onboarding services; deleting a
whole project (with its rules) is an admin decision. The matrix is enforced by
`require_org` and checked route by route in `services/api/tests/test_tenant_matrix.py`
(every org-scoped route must be listed there, or that test fails).

## 6. Rate limits and request hygiene

Postgres token buckets (`rate_limit_buckets`, one UPSERT per decision; Proposed ADR B).
Refused requests get `429 rate_limited` with `Retry-After` (seconds).

| Bucket | Key | Default | Settings |
|---|---|---|---|
| `auth` | client IP | burst 20, 10/min | `RL_RATE_LIMIT_AUTH_BURST`, `…_AUTH_PER_MINUTE` — signup, verify, resend, login, reset request/confirm, GitHub authorize/callback |
| `login_account` | sha256(e-mail) + IP | burst 5, 1/min | `RL_RATE_LIMIT_LOGIN_ACCOUNT_*` |
| `ingest` | API key id | burst 200, 3000/min | `RL_RATE_LIMIT_INGEST_*` — `POST /api/v1/deployments`; reuse `limit_api_key(scope_dep)` for `/v1/traces` |

* **Client IP** (`rl_api.clientip`): the TCP peer, unless it is in `RL_TRUSTED_PROXIES`
  (CIDRs); then `X-Forwarded-For` is walked from the right to the first untrusted hop. The
  same value goes into the audit log. Note: compose sets uvicorn's
  `RL_FORWARDED_ALLOW_IPS="*"` for local development; in production set it (and
  `RL_TRUSTED_PROXIES`) to the load balancer's range only.
* **Security headers** on every response: `Content-Security-Policy: default-src 'none';
  frame-ancestors 'none'; base-uri 'none'; form-action 'none'` (a relaxed policy only for
  `/docs`, which is disabled in prod), `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy:
  same-origin`, `Permissions-Policy`, and `Strict-Transport-Security: max-age=63072000;
  includeSubDomains` whenever `RL_ENV≠dev`.
* **Body limit**: `RL_MAX_BODY_BYTES` (1 MiB) for declared and chunked bodies → `413
  payload_too_large`.
* **CORS**: explicit `RL_CORS_ORIGINS` only (a `*` is refused at startup), credentials
  allowed, headers `Authorization, Content-Type, X-Request-ID, X-CSRF-Token, X-RL-Org`,
  exposes `X-Request-ID, Retry-After`.

## 7. Secrets at rest

* Passwords: Argon2id. API keys, session/refresh tokens, e-mailed tokens, OAuth state:
  SHA-256 of 256-bit random values (ADR-0016 reasoning).
* Slack webhook URLs: AES-256-GCM (`rl_db.secretbox`), key = HKDF-SHA256(`RL_SECRET_KEY`,
  purpose label), random 96-bit nonce, associated data
  `alert_rules.slack_webhook_url:<tenant_id>:<rule_id>`; token `v1.<kid>.<b64>`. Only a hint
  (`https://hooks.slack.com/…abcd`) is ever returned. Rotation: put the old key in
  `RL_SECRET_KEY_PREVIOUS`; `SecretBox.needs_reencrypt` finds rows to rewrite.
* `RL_SECRET_KEY` (≥ 32 characters) is required when `RL_ENV=prod` (API and worker refuse to
  start without it, or with the public dev key). Dev/test fall back to a public
  development key that protects nothing of value.

## 8. Audit log

`audit_log` is insert-only: triggers raise on `UPDATE`, `DELETE` and `TRUNCATE`. Each entry
has the tenant (NULL for sign-in events), actor type (`user | api_key | system |
anonymous`), actor user / key id and e-mail, action, target type and id, client IP (trusted
proxy aware), user agent, timestamp and a JSON `metadata` object (never a secret: keys are
identified by prefix, webhooks as `set/removed`, failed sign-ins by an e-mail fingerprint
`sha256(lower(email))[:16]`). Entries commit in the same transaction as the change.

Actions: `auth.signed_up`, `auth.email_verified`, `auth.login_succeeded`,
`auth.login_failed`, `auth.logout`, `auth.refresh_token_reused`, `auth.password_reset`,
`auth.github_linked`, `org.created`, `org.settings_changed`, `org.deleted`,
`member.invited`, `member.invite_revoked`, `member.joined`, `member.role_changed`,
`member.removed`, `api_key.created`, `api_key.revoked`, `api_key.rotated`,
`project.created|updated|deleted`, `alert_rule.created|updated|deleted`,
`billing.checkout_started`, `billing.subscription_changed`, `billing.payment_failed`,
`model.promoted` (hook `rl_api.audit.model_promoted()` for the M4 registry).

API (owner/admin): `GET /api/v1/orgs/{org_id}/audit-log?action&actor_user_id&target_type&target_id&since&until&limit&cursor`
(newest first, keyset cursor) and `GET …/audit-log/export.csv` (same filters, ≤ 50,000 rows,
RFC 4180 quoting, cells starting with `= + - @ \t \r` prefixed with `'` against formula
injection). The log survives organization deletion (no foreign key). Tenant-less sign-in
entries are operator-visible only for now.

## 9. Alerts and notifications

Per-project alert rules (admin+): triggers `on_regression` (severity band ≥
`regression_min_band`, HIGH or CRITICAL) and `on_risk` (pre-deploy band ≥ `risk_min_band`);
channels: one Slack webhook (Team plan and up; `402 plan_limit_reached` on Free) and up to 20
e-mail recipients. The worker's `AnalysisResultsConsumer` reads `analysis.results`
(`rl_core.events.analysis.parse_analysis`, docs/events/ANALYSIS_RESULTS.md), maps the
service to its project, and queues one `notification_log` row per (rule, deployment, kind,
channel) with `ON CONFLICT DO NOTHING`, so redeliveries and the early/final analyses of
one deployment alert once. `notify_risk()` is the hook for pre-deploy risk. The
`NotificationDispatcher` sends with retries (full-jitter backoff, Slack `Retry-After`),
marks permanent failures and exhausted rows `dead` (the notifier's dead-letter queue,
`GET /api/v1/orgs/{org_id}/notifications?status=dead`); malformed events go to
`analysis.results.dlq`.

## 10. Open issues

1. Postgres RLS (ADR-0010 item 3) is not enabled yet: isolation rests on repository
   filters, composite foreign keys and the test matrix.
2. Tenant-less sign-in audit entries have no API (a "my security activity" view).
3. An analysis escalating HIGH → CRITICAL after the first alert does not re-alert.
4. Delivery is at least once: a dispatcher crash after Slack accepted a message re-sends
   it after the lease (5 min).
5. No session-management UI (list/revoke other sessions); password reset revokes all.
6. MFA, breached-password k-anonymity lookups (network) and CAPTCHA are not implemented.

---

## Proposed ADR A — Browser sessions: opaque, DB-backed tokens with rotating refresh tokens

- **Context.** The web app needs a login that is revocable at once (logout, password reset,
  stolen-token response) and safe against XSS token theft and CSRF. The API already does a
  database lookup per API-key request.
- **Options.** (1) Signed JWT access tokens + refresh tokens: no lookup per request, but
  revocation waits for expiry unless a deny-list is kept (which is a lookup again), plus key
  management and algorithm pitfalls. (2) **Opaque random tokens whose SHA-256 is looked up**.
- **Decision.** Option 2. A 256-bit `rla_` access token in an `HttpOnly; Secure;
  SameSite=Lax` cookie, valid 15 minutes, looked up by hash on every request (one indexed
  query, same cost as API keys); a 256-bit `rlr_` refresh token (14 days, cookie path
  `/api/v1/auth`), rotated on every use, with the session row as the token family: presenting
  a rotated token revokes the family. A 30-day absolute session limit. CSRF by a
  session-bound double-submit token derived with HMAC from `RL_SECRET_KEY`. API keys stay
  the machine credential and are CSRF-exempt.
- **Consequences.** Logout, reset and reuse detection take effect on the next request.
  Two concurrent refreshes from one browser look like theft, so the front-end must
  serialise refreshes. The database is on the path of every authenticated request (it
  already was). No signing keys to rotate.

## Proposed ADR B — Rate limiting without Redis: Postgres token buckets

- **Context.** Auth endpoints and ingest need limits that hold across several API
  processes. ADR-0011 keeps Redis out of the stack.
- **Options.** In-process counters (wrong with more than one process); a fixed window in
  Postgres (simple, but allows 2× bursts at window edges and needs per-window rows); **a
  token bucket in Postgres**; Redis (a new component to run, secure and monitor).
- **Decision.** One row per bucket in `rate_limit_buckets` updated by a single
  `INSERT … ON CONFLICT DO UPDATE … RETURNING`: tokens refill continuously at the policy's
  rate up to its burst, a request takes one whole token, and a refused request takes
  nothing (so a client is never locked out beyond `1/rate` after it stops). The row lock
  serialises concurrent requests exactly; the database clock (`clock_timestamp()`) is the
  only clock. Decisions commit in their own short transaction. Bucket keys hash their
  subject (no e-mails or key ids in clear). The worker deletes buckets idle for a day.
- **Consequences.** One extra small write per limited request (cost not benchmarked yet).
  Hot keys serialise on one row, which is the point. If
  Postgres is down, limited endpoints fail, as they would anyway. Revisit with a dedicated
  limiter if ingest exceeds what one row-lock per request can sustain (to be measured with
  k6 in M12).

## Proposed ADR C — Integration secrets (Slack webhook URLs) encrypted at rest

- **Context.** A Slack incoming-webhook URL lets anyone post to the customer's channel. It
  must be stored, sent by the worker, and never shown again; a database dump must not
  reveal it; and the notifier must not become an SSRF tool.
- **Decision.** AES-256-GCM via `cryptography`, a fresh 96-bit nonce per encryption, the key
  derived with HKDF-SHA256 from `RL_SECRET_KEY` and a purpose label (so CSRF and encryption
  keys are independent), and associated data binding each ciphertext to its tenant, rule
  and column. Ciphertexts name their key id; `RL_SECRET_KEY_PREVIOUS` keeps old keys for
  decryption during rotation. Only a short hint is stored in clear and returned. The URL
  must be `https://hooks.slack.com/services/…` (any http(s) host only in dev/test), checked
  on save and again before each send; requests use a timeout and never follow redirects.
- **Consequences.** API and worker must share `RL_SECRET_KEY`; losing it makes stored
  webhooks unrecoverable (rows go `dead` with a clear error; admins re-enter the URL). A
  copied ciphertext is useless in another row. A KMS-backed key is the production upgrade
  path (same token format, different key source).
