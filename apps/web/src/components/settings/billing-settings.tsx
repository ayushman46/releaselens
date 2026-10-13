"use client";

import { Check, CreditCard, ExternalLink } from "lucide-react";
import { useSearchParams } from "next/navigation";
import { useState } from "react";

import { ApiErrorState } from "@/components/rl/api-error-state";
import { StatusPill } from "@/components/rl/status-pill";
import { useSession } from "@/components/session/session-provider";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { LoadingState } from "@/components/ui/skeleton";
import { api, unwrap } from "@/lib/api/client";
import { ApiError, describeApiError } from "@/lib/api/errors";
import { useBilling, usePlans } from "@/lib/api/queries";
import type { PlanLimitsOut } from "@/lib/api/types";
import { formatCount, formatDate } from "@/lib/format";

export function billingErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "billing_not_configured" || error.status === 503) return "Billing not configured: this server has no Stripe key, so plans cannot be bought here yet.";
    if (error.code === "already_subscribed") return "This organization already has an active subscription. Manage it in the customer portal.";
    if (error.code === "no_billing_account") return "There is no billing account yet: start a subscription first.";
    if (error.status === 403) return "Only an owner can change the plan.";
  }
  return describeApiError(error);
}

function usageLine(used: number, max: number | null | undefined, unit: string) {
  return max === null || max === undefined ? `${formatCount(used)} ${unit} (no fixed limit)` : `${formatCount(used)} of ${formatCount(max)} ${unit}`;
}

function PlanCard({ plan, current, canBuy, onBuy, pending }: { plan: PlanLimitsOut; current: boolean; canBuy: boolean; onBuy: () => void; pending: boolean }) {
  const features = [
    plan.max_projects === null ? "Projects by contract" : `${plan.max_projects} project${plan.max_projects === 1 ? "" : "s"}`,
    plan.max_services === null ? "Services by contract" : `${plan.max_services} services`,
    `${plan.retention_days}-day retention`,
    plan.slack_alerts ? "Slack alerts" : "E-mail alerts only",
    plan.github_checks ? "GitHub Checks" : null,
  ].filter((f): f is string => Boolean(f));
  return (
    <li className={`flex flex-col rounded-xl border bg-panel p-5 shadow-panel ${current ? "border-brand" : ""}`}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold">{plan.name}</h3>
        {current ? <StatusPill tone="info" label="Current plan" size="sm" icon={Check} /> : null}
      </div>
      <p className="mt-2 text-2xl font-semibold tracking-tight">
        {plan.price_usd_month === null ? "Custom" : `$${plan.price_usd_month}`}
        {plan.price_usd_month !== null ? <span className="text-sm font-normal text-muted-foreground"> / month</span> : null}
      </p>
      <ul className="mt-3 flex-1 space-y-1.5 text-sm text-foreground-secondary">
        {features.map((f) => (
          <li key={f} className="flex gap-2">
            <Check aria-hidden="true" className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
            {f}
          </li>
        ))}
      </ul>
      {!current && plan.self_serve && plan.price_usd_month ? (
        <Button className="mt-4" size="sm" variant="brand" onClick={onBuy} disabled={!canBuy || pending}>
          {pending ? "Opening checkout…" : `Upgrade to ${plan.name}`}
        </Button>
      ) : !current && !plan.self_serve ? (
        <p className="mt-4 text-xs text-muted-foreground">By contract: contact sales.</p>
      ) : null}
    </li>
  );
}

export function BillingSettings() {
  const session = useSession();
  const orgId = session.orgId!;
  const params = useSearchParams();
  const billing = useBilling(orgId);
  const plans = usePlans();
  const isOwner = session.can("owner");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const checkout = params.get("checkout");

  /** Only Team is self-serve (CheckoutIn.plan is the literal "team"). */
  const go = async (kind: "checkout" | "portal", plan?: string) => {
    setPending(kind === "checkout" ? (plan ?? "team") : "portal");
    setError(null);
    try {
      const { url } =
        kind === "checkout"
          ? await unwrap(api.POST("/api/v1/orgs/{org_id}/billing/checkout", { params: { path: { org_id: orgId } }, body: { plan: "team" } }))
          : await unwrap(api.POST("/api/v1/orgs/{org_id}/billing/portal", { params: { path: { org_id: orgId } } }));
      window.location.assign(url);
    } catch (e) {
      setError(billingErrorMessage(e));
      setPending(null);
    }
  };

  if (billing.isPending) return <LoadingState label="Loading billing…" lines={4} />;
  if (billing.isError) return <ApiErrorState what="Billing" error={billing.error} onRetry={() => void billing.refetch()} />;
  const b = billing.data;
  const sub = b.subscription;

  return (
    <div className="space-y-6">
      {checkout === "success" ? (
        <Callout tone="healthy" title="Checkout complete">
          Stripe confirmed the payment. The plan changes as soon as Stripe&apos;s webhook arrives, usually within seconds; refresh if it still shows the old plan.
        </Callout>
      ) : checkout === "cancelled" ? (
        <Callout tone="info">Checkout was cancelled. Nothing was charged.</Callout>
      ) : null}
      {!b.stripe_configured ? (
        <Callout tone="warning" title="Billing not configured">
          This server has no Stripe test key, so checkout and the customer portal are unavailable. Your plan and usage are shown below.
        </Callout>
      ) : null}
      {error ? <Callout tone="critical">{error}</Callout> : null}

      <section aria-labelledby="usage-title" className="rounded-xl border bg-panel p-5 shadow-panel">
        <div className="flex flex-wrap items-center gap-3">
          <CreditCard aria-hidden="true" className="size-4 text-muted-foreground" />
          <h2 id="usage-title" className="text-sm font-semibold">
            {b.limits.name} plan
          </h2>
          {sub ? (
            <StatusPill
              tone={sub.payment_failed_at ? "critical" : sub.status === "active" || sub.status === "trialing" ? "healthy" : "warning"}
              label={sub.payment_failed_at ? "Payment failed" : `Subscription ${sub.status}`}
              size="sm"
            />
          ) : null}
          {sub && isOwner && b.stripe_configured ? (
            <Button variant="outline" size="sm" className="ml-auto" onClick={() => void go("portal")} disabled={pending !== null}>
              <ExternalLink aria-hidden="true" />
              {pending === "portal" ? "Opening…" : "Manage subscription"}
            </Button>
          ) : null}
        </div>
        <ul className="mt-3 grid gap-2 text-sm text-foreground-secondary sm:grid-cols-3">
          <li>{usageLine(b.usage.projects, b.limits.max_projects, "projects")}</li>
          <li>{usageLine(b.usage.services, b.limits.max_services, "services")}</li>
          <li>{formatCount(b.usage.deployments_this_month)} deployments this month</li>
        </ul>
        {sub ? (
          <p className="mt-2 text-xs text-muted-foreground">
            {sub.cancel_at_period_end ? "Cancels" : "Renews"} {formatDate(sub.current_period_end)}
            {sub.payment_failed_at ? ` · payment failed ${formatDate(sub.payment_failed_at)}; Stripe is retrying` : ""}
          </p>
        ) : null}
      </section>

      <section aria-labelledby="plans-title">
        <h2 id="plans-title" className="mb-3 text-sm font-semibold">
          Plans
        </h2>
        {!isOwner ? <p className="mb-3 text-sm text-muted-foreground">Only an owner can change the plan.</p> : null}
        {plans.isPending ? (
          <LoadingState label="Loading plans…" />
        ) : plans.isError ? (
          <ApiErrorState what="Plans" error={plans.error} />
        ) : (
          <ul className="grid gap-4 md:grid-cols-3">
            {plans.data.map((p) => (
              <PlanCard
                key={p.plan}
                plan={p}
                current={p.plan === b.plan}
                canBuy={isOwner}
                pending={pending === p.plan}
                onBuy={() => void go("checkout", p.plan)}
              />
            ))}
          </ul>
        )}
        <p className="mt-3 text-xs text-muted-foreground">Stripe runs in test mode. A downgrade never deletes existing projects or services; only new ones are refused.</p>
      </section>
    </div>
  );
}
