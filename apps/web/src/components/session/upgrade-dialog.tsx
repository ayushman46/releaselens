"use client";

import { Sparkles } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import type { PlanLimitDetails } from "@/lib/api/errors";

const LIMIT_LABEL: Record<string, string> = {
  projects: "projects",
  services: "services",
  slack_alerts: "Slack alerts",
  github_checks: "GitHub Checks",
};

export function planLimitSentence(details: PlanLimitDetails): string {
  const what = LIMIT_LABEL[details.limit] ?? details.limit.replace(/_/g, " ");
  const plan = details.plan.charAt(0).toUpperCase() + details.plan.slice(1);
  if (details.max !== null && details.max !== undefined) {
    return `The ${plan} plan allows ${details.max} ${what}${details.current !== null && details.current !== undefined ? ` and this organization has ${details.current}` : ""}.`;
  }
  return `${what.charAt(0).toUpperCase() + what.slice(1)} are not included in the ${plan} plan.`;
}

/** Shown whenever the API answers `402 plan_limit_reached`, wherever the action started. */
export function UpgradeDialog({
  details,
  onClose,
  canUpgrade,
}: {
  details: PlanLimitDetails | null;
  onClose: () => void;
  canUpgrade: boolean;
}) {
  if (!details) return null;
  const target = details.upgrade?.plan ? details.upgrade.plan.charAt(0).toUpperCase() + details.upgrade.plan.slice(1) : "Team";
  return (
    <Dialog open onClose={onClose} label="Plan limit reached" className="p-6">
      <div className="flex size-10 items-center justify-center rounded-lg border bg-subtle">
        <Sparkles aria-hidden="true" className="size-5 text-brand-text" />
      </div>
      <h2 className="mt-4 text-base font-semibold">Plan limit reached</h2>
      <p className="mt-2 text-sm leading-6 text-muted-foreground">
        {planLimitSentence(details)} Nothing was created. Upgrade to {target} to raise the limit; existing resources are never removed by a plan change.
      </p>
      {!canUpgrade ? (
        <p className="mt-2 text-sm leading-6 text-muted-foreground">Only an owner of this organization can change the plan.</p>
      ) : null}
      <div className="mt-6 flex flex-wrap justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={onClose}>
          Not now
        </Button>
        <Button asChild size="sm" variant="brand">
          <Link href="/app/settings/billing" onClick={onClose}>
            {canUpgrade ? `Upgrade to ${target}` : "See plans"}
          </Link>
        </Button>
      </div>
    </Dialog>
  );
}
