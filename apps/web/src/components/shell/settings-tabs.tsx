"use client";

import { Bell, Building, CreditCard, GitPullRequest, KeyRound, Plug, Radio } from "lucide-react";
import Link from "next/link";

import { StatusPill } from "@/components/rl/status-pill";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { EmptyState } from "@/components/ui/empty-state";
import { KeyValueList } from "@/components/ui/key-value";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

/** Shape of an API key row once M10 serves them (only the prefix is ever shown). */
interface ApiKeyRow {
  id: string;
  name: string;
  prefix: string;
  scopes: string[];
  createdAt: string;
  lastUsedAt: string | null;
}

const API_KEY_COLUMNS: DataTableColumn<ApiKeyRow>[] = [
  { id: "name", header: "Name", cell: (r) => r.name, sortValue: (r) => r.name, rowHeader: true },
  { id: "prefix", header: "Key", cell: (r) => <code className="font-mono text-xs">{r.prefix}…</code> },
  { id: "scopes", header: "Scopes", cell: (r) => r.scopes.join(", ") },
  { id: "created", header: "Created", cell: (r) => r.createdAt, sortValue: (r) => r.createdAt },
  {
    id: "lastUsed",
    header: "Last used",
    cell: (r) => r.lastUsedAt ?? "Never",
    sortValue: (r) => r.lastUsedAt,
  },
];

const INTEGRATIONS = [
  {
    name: "GitHub App",
    icon: GitPullRequest,
    description: "Receives pull-request webhooks and posts risk scores as Check Runs.",
    milestone: "M5",
  },
  {
    name: "OTLP endpoint",
    icon: Radio,
    description: "Receives traces from your OpenTelemetry Collector or SDKs.",
    milestone: "M7",
  },
  {
    name: "Slack alerts",
    icon: Bell,
    description: "Posts detected regressions with their probable origin to a channel.",
    milestone: "M10",
  },
];

export function SettingsTabs() {
  return (
    <Tabs defaultValue="general">
      <TabsList aria-label="Settings sections" className="w-full">
        <TabsTrigger value="general">
          <Building aria-hidden="true" />
          General
        </TabsTrigger>
        <TabsTrigger value="api-keys">
          <KeyRound aria-hidden="true" />
          API keys
        </TabsTrigger>
        <TabsTrigger value="integrations">
          <Plug aria-hidden="true" />
          Integrations
        </TabsTrigger>
        <TabsTrigger value="billing">
          <CreditCard aria-hidden="true" />
          Billing
        </TabsTrigger>
      </TabsList>

      <TabsContent value="general" className="pt-6">
        <Callout tone="info" title="Organizations arrive with sign-in in M10">
          Until then there is no organization to configure. These settings will hold the organization name,
          members and their roles.
        </Callout>
        <div className="mt-6 rounded-xl border bg-panel px-5 shadow-panel">
          <KeyValueList
            items={[
              { label: "Organization", value: <span className="text-muted-foreground">Not created yet</span> },
              { label: "Members", value: <span className="text-muted-foreground">Not created yet</span> },
              { label: "Default environment", value: <span className="text-muted-foreground">Set after your first deployment event</span> },
            ]}
          />
        </div>
      </TabsContent>

      <TabsContent value="api-keys" className="pt-6">
        <DataTable<ApiKeyRow>
          caption="API keys"
          columns={API_KEY_COLUMNS}
          rows={[]}
          getRowId={(r) => r.id}
          emptyState={
            <EmptyState
              variant="bare"
              headingLevel={3}
              icon={KeyRound}
              title="No API keys yet"
              description="Keys are created here once sign-in ships (M10). They are stored hashed; only a short prefix stays visible. Scopes: risk:write, deployments:write, telemetry:write."
            />
          }
        />
      </TabsContent>

      <TabsContent value="integrations" className="pt-6">
        <ul className="divide-y rounded-xl border bg-panel shadow-panel">
          {INTEGRATIONS.map((integration) => {
            const Icon = integration.icon;
            return (
              <li key={integration.name} className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center">
                <div className="flex min-w-0 flex-1 items-start gap-3.5">
                  <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-lg border bg-subtle">
                    <Icon aria-hidden="true" className="size-4 text-foreground-secondary" />
                  </span>
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="text-sm font-medium">{integration.name}</h3>
                      <Badge>Available in {integration.milestone}</Badge>
                    </div>
                    <p className="mt-0.5 text-sm text-muted-foreground">{integration.description}</p>
                  </div>
                </div>
                <div className="pl-11 sm:pl-0">
                  <StatusPill tone="unknown" label="Not connected" size="sm" />
                </div>
              </li>
            );
          })}
        </ul>
      </TabsContent>

      <TabsContent value="billing" className="pt-6">
        <EmptyState
          icon={CreditCard}
          title="Billing opens in M10"
          description="There is nothing to pay for yet. Plans are Free, Team at $99 per month, and Enterprise."
          action={
            <Button asChild size="sm" variant="outline">
              <Link href="/pricing">See plans</Link>
            </Button>
          }
        />
      </TabsContent>
    </Tabs>
  );
}
