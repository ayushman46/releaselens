# Billing and plan limits (M10)

Stripe runs in **test mode only**: the API refuses to start with a key that does not begin
with `sk_test_` or `rk_test_`. Without `RL_STRIPE_SECRET_KEY` the Checkout and Portal
endpoints answer `503 billing_not_configured`, and without `RL_STRIPE_WEBHOOK_SECRET` the
webhook answers `503`; nothing pretends a payment succeeded. Owner setup steps:
[`docs/BLOCKERS.md`](../BLOCKERS.md) **B2**.

## Plans and limits

Single source of truth: `packages/rl_core/src/rl_core/plans.py` (also served publicly at
`GET /api/v1/plans`, matching `apps/web/src/lib/pricing.ts`).

| | Free | Team | Enterprise |
|---|---|---|---|
| Price | $0 | $99 / month (Stripe price `RL_STRIPE_PRICE_TEAM`) | contact (no self-serve checkout) |
| Projects | 1 | 10 | by contract (no fixed limit) |
| Services | 3 | 50 | by contract |
| Retention | 7 days | 30 days | 90 days until a contract says otherwise |
| Slack alerts | – (e-mail alerts only) | ✓ | ✓ |
| GitHub Checks | – | ✓ | ✓ |

**Enforcement (server-side).**

* Projects and services: counted when one is created, under a lock on the organization row
  (two concurrent creates cannot both pass). Over the limit → `402` with
  `{"error": {"code": "plan_limit_reached", "details": {"limit": "projects", "plan": "free",
  "max": 1, "current": 1, "upgrade": {"plan": "team", "url": "<web>/app/settings/billing"}}}}`.
  A downgrade keeps existing resources; only new ones are refused.
* Slack alerts: setting a webhook on a plan without them → `402 plan_limit_reached`
  (`limit: "slack_alerts"`); the dispatcher also re-checks the plan before every send and
  marks Slack rows `skipped` after a downgrade (e-mail keeps working).
* Retention: the worker's housekeeping loop deletes deployments (and, by cascade, their
  analysis jobs) older than the plan's retention, hourly, in bounded batches; demo
  organizations are exempt. Span partitions (M7) are dropped by the telemetry retention job
  using the same `retention_days`.
* GitHub Checks: the flag is exposed in the limits; the Check Run publisher (M5, GitHub App
  pending, B1) must consult `limits_for(plan).github_checks`.
* Usage: `usage_counters` keeps a monthly `deployments` count (accepted ingest events),
  shown in `GET /api/v1/orgs/{org_id}/billing`. It is informational; no plan limits it yet.

## Flows

* **Status** `GET /api/v1/orgs/{org_id}/billing` (any member): plan, limits, usage
  (projects, services, deployments this month), subscription (status, period end,
  `cancel_at_period_end`, `payment_failed_at`), `stripe_configured`.
* **Checkout** `POST /api/v1/orgs/{org_id}/billing/checkout {"plan": "team"}` (owner):
  creates a Checkout Session in `subscription` mode for `RL_STRIPE_PRICE_TEAM` with
  `client_reference_id` and `metadata.tenant_id` = the org id (also on the subscription),
  the existing customer or the owner's e-mail, and success/cancel URLs under
  `RL_WEB_BASE_URL/app/settings/billing`. Returns `{"url"}`; `409 already_subscribed` when a
  subscription is active.
* **Customer Portal** `POST /api/v1/orgs/{org_id}/billing/portal` (owner): `{"url"}`;
  `409 no_billing_account` before the first checkout.
* **Webhook** `POST /api/v1/billing/stripe/webhook`: the raw body is verified against
  `Stripe-Signature` with `stripe.WebhookSignature.verify_header` (HMAC-SHA256 over
  `"{t}.{payload}"`, 5-minute tolerance) before parsing; bad or stale signatures → `400
  invalid_signature`. In **one transaction** the event id is inserted into `stripe_events`
  (`ON CONFLICT DO NOTHING`: a redelivery answers `{"status": "duplicate"}`) and the event is
  applied; if anything fails the transaction rolls back and Stripe's retry starts over.

| Event | Effect |
|---|---|
| `checkout.session.completed` (mode `subscription`) | Find the org by `client_reference_id`/metadata; upsert `subscriptions` (customer, subscription, `active`, Team); `organizations.plan = team` |
| `customer.subscription.created` / `.updated` | Status and price from the subscription (period end from its first item, API 2025-03+ layout); `active`, `trialing`, `past_due` → Team (or Enterprise when `metadata.rl_plan=enterprise`); anything else → Free |
| `customer.subscription.deleted` | Status `canceled`, plan Free |
| `invoice.payment_failed` | `payment_failed_at` set (plan unchanged: `past_due` keeps access while Stripe retries) |
| anything else, or an unknown customer/org | Acknowledged and ignored (`{"status": "ignored"}`) |

Stripe does not guarantee ordering: `subscriptions.last_event_at` stores the `created` time
of the last applied event, and an older event never overwrites newer state. Each applied
change writes an audit entry (`billing.subscription_changed`, `billing.payment_failed`,
actor `system`). Deleting an organization is refused while its subscription is live.

## Local test-mode setup (owner, B2)

1. Stripe Dashboard, **test mode**: create product "Team" with a recurring $99/month price;
   copy its `price_…` id.
2. Developers → API keys: copy the test secret key `sk_test_…`.
3. Webhooks: either add an endpoint pointing at your tunnel's
   `/api/v1/billing/stripe/webhook`, or run `stripe listen --forward-to
   localhost:28000/api/v1/billing/stripe/webhook` and copy the printed `whsec_…`. Subscribe to
   `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`.
4. Set in `infra/compose/.env`: `RL_STRIPE_SECRET_KEY`, `RL_STRIPE_WEBHOOK_SECRET`,
   `RL_STRIPE_PRICE_TEAM`. Enable the Customer Portal in the dashboard (test mode).
5. Pay with Stripe's test card `4242 4242 4242 4242`, any future expiry, any CVC.

The SDK is pinned (`stripe==15.6.1`, ADR-0014), which fixes the API version to
`2026-08-26.dahlia`. Tests never call Stripe: they inject a fake gateway and sign webhook
fixtures with the SDK's `WebhookSignature.generate_signature_header`
(`services/api/tests/test_billing_api.py`).
