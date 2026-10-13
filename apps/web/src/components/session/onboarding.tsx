"use client";

import { ArrowRight, Building, KeyRound, Radio, Rocket } from "lucide-react";
import Link from "next/link";

import { CreateOrgForm } from "@/components/session/create-org-form";
import { useSession } from "@/components/session/session-provider";
import { StatusPill } from "@/components/rl/status-pill";
import { PageHeader } from "@/components/ui/page-header";

const NEXT_STEPS = [
  {
    title: "Create an API key",
    description: "Scopes deployments:write and telemetry:write for your pipeline and Collector; risk:write for the GitHub Action.",
    href: "/app/settings/api-keys",
    icon: KeyRound,
  },
  {
    title: "Send traces",
    description: "Point your OpenTelemetry Collector at the OTLP endpoint. Services and the service map come from these traces.",
    href: "/app/services",
    icon: Radio,
  },
  {
    title: "Report deployments",
    description: "Post one event per release. Each one starts a before/after comparison of that service's traces.",
    href: "/app/deployments",
    icon: Rocket,
  },
];

/** First run: create the organization, then the three connections. */
export function Onboarding() {
  const session = useSession();
  const hasOrg = Boolean(session.activeOrg);
  const firstName = session.me?.user.display_name?.split(" ")[0];

  return (
    <>
      <PageHeader
        title={hasOrg ? "Connect your data" : `Welcome${firstName ? `, ${firstName}` : ""}`}
        description={
          hasOrg
            ? "Your organization is ready. ReleaseLens shows only what your systems send it, so connect them next."
            : "Create your organization first. You can invite teammates and add more organizations later."
        }
      />
      <ol className="space-y-4">
        <li className="rounded-xl border bg-panel p-5 shadow-panel">
          <div className="flex flex-wrap items-center gap-3">
            <span className="flex size-8 items-center justify-center rounded-lg border bg-subtle">
              <Building aria-hidden="true" className="size-4 text-foreground-secondary" />
            </span>
            <h2 className="text-sm font-semibold">
              <span className="sr-only">Step 1: </span>Create your organization
            </h2>
            {hasOrg ? <StatusPill tone="healthy" label={`Done: ${session.activeOrg!.name}`} size="sm" /> : null}
          </div>
          {hasOrg ? null : (
            <div className="mt-4 max-w-md">
              <CreateOrgForm submitLabel="Create and continue" />
            </div>
          )}
        </li>
        {NEXT_STEPS.map((step, index) => {
          const Icon = step.icon;
          return (
            <li key={step.title} className="flex flex-col gap-3 rounded-xl border bg-panel p-5 shadow-panel sm:flex-row sm:items-center">
              <div className="flex min-w-0 flex-1 items-start gap-3">
                <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border bg-subtle">
                  <Icon aria-hidden="true" className="size-4 text-foreground-secondary" />
                </span>
                <div className="min-w-0">
                  <h2 className="text-sm font-semibold">
                    <span className="sr-only">Step {index + 2}: </span>
                    {step.title}
                  </h2>
                  <p className="mt-0.5 text-sm leading-6 text-muted-foreground">{step.description}</p>
                </div>
              </div>
              {hasOrg ? (
                <Link href={step.href} className="inline-flex items-center gap-1 pl-11 text-sm font-medium text-brand-text hover:underline sm:pl-0">
                  Set up
                  <span className="sr-only">: {step.title}</span>
                  <ArrowRight aria-hidden="true" className="size-3.5" />
                </Link>
              ) : (
                <span className="pl-11 text-xs text-muted-foreground sm:pl-0">After step 1</span>
              )}
            </li>
          );
        })}
      </ol>
      {hasOrg ? (
        <p className="mt-6 text-sm">
          <Link href="/app" className="font-medium text-brand-text hover:underline">
            Go to the overview
          </Link>
        </p>
      ) : null}
    </>
  );
}
