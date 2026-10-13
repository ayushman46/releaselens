import { Boxes, Gauge, Network, Rocket, ScanSearch } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { EmptyState } from "@/components/ui/empty-state";
import { KeyValueList } from "@/components/ui/key-value";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { API_BASE_URL } from "@/lib/env";
import { deploymentCurl, GITHUB_ACTION_YAML, otelCollectorYaml, otelEnv } from "@/lib/snippets";

/**
 * What each list shows when the organization has no data yet: how to make data appear. These
 * never contain sample rows.
 */

export function DeploymentsSetup({ headingLevel = 2 }: { headingLevel?: 2 | 3 }) {
  return (
    <EmptyState
      icon={Rocket}
      headingLevel={headingLevel}
      title="Send your first deployment event"
      description="ReleaseLens never deploys code. Your pipeline tells it when a release went out, and each event starts a before/after trace comparison for that service."
      action={
        <Button asChild size="sm">
          <Link href="/app/settings/api-keys">Create an API key</Link>
        </Button>
      }
      secondaryAction={
        <Button asChild size="sm" variant="ghost">
          <Link href="/docs/deployment-api">Read the guide</Link>
        </Button>
      }
    >
      <CodeBlock code={deploymentCurl(API_BASE_URL)} title="POST /api/v1/deployments" />
      <div className="mt-4 rounded-lg border bg-panel px-4">
        <KeyValueList
          labelWidth="sm:grid-cols-[minmax(0,9rem)_minmax(0,1fr)]"
          items={[
            {
              label: "Authentication",
              value: (
                <>
                  <code className="font-mono text-xs">x-releaselens-key</code> header with an API key that has the{" "}
                  <code className="font-mono text-xs">deployments:write</code> scope.
                </>
              ),
            },
            {
              label: "Response",
              value: (
                <>
                  <code className="font-mono text-xs">202 Accepted</code> with an <code className="font-mono text-xs">event_id</code>.
                  Retrying the same event never creates a duplicate.
                </>
              ),
            },
          ]}
        />
      </div>
    </EmptyState>
  );
}

export function ServicesSetup({ headingLevel = 2 }: { headingLevel?: 2 | 3 }) {
  return (
    <EmptyState
      icon={Boxes}
      headingLevel={headingLevel}
      title="No services reporting"
      description={
        <>
          Services appear automatically when spans arrive at the OTLP endpoint, named by their{" "}
          <code className="font-mono text-xs text-foreground-secondary">service.name</code> resource attribute. Point your
          OpenTelemetry Collector, or your SDKs directly, at ReleaseLens with a key that has the{" "}
          <code className="font-mono text-xs">telemetry:write</code> scope.
        </>
      }
      action={
        <Button asChild size="sm">
          <Link href="/app/settings/api-keys">Create an API key</Link>
        </Button>
      }
      secondaryAction={
        <Button asChild size="sm" variant="ghost">
          <Link href="/docs/otel-setup">Read the setup guide</Link>
        </Button>
      }
    >
      <Tabs defaultValue="collector">
        <TabsList aria-label="Setup method">
          <TabsTrigger value="collector">Collector</TabsTrigger>
          <TabsTrigger value="sdk">SDK environment</TabsTrigger>
        </TabsList>
        <TabsContent value="collector">
          <CodeBlock code={otelCollectorYaml(API_BASE_URL)} title="otel-collector.yaml" />
        </TabsContent>
        <TabsContent value="sdk">
          <CodeBlock code={otelEnv(API_BASE_URL)} title="environment" />
        </TabsContent>
      </Tabs>
    </EmptyState>
  );
}

export function RegressionsSetup() {
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <EmptyState
        icon={ScanSearch}
        title="No regressions found"
        description="After each deployment, ReleaseLens compares the affected services before and after the release. A deployment is listed here only when its latest analysis is Regressed or Warning. With too little traffic the verdict is “Insufficient data”, not a guess."
        action={
          <Button asChild size="sm">
            <Link href="/app/deployments">See all deployments</Link>
          </Button>
        }
        secondaryAction={
          <Button asChild size="sm" variant="ghost">
            <Link href="/docs/methodology">How detection works</Link>
          </Button>
        }
      />
      <DetectionDesign />
    </div>
  );
}

export function DetectionDesign() {
  return (
    <section aria-labelledby="engine-title" className="self-start rounded-xl border bg-panel shadow-panel">
      <h2 id="engine-title" className="border-b px-5 py-3.5 text-sm font-semibold">
        How a regression is called (engine v1)
      </h2>
      <div className="px-5">
        <KeyValueList
          labelWidth="sm:grid-cols-[minmax(0,7.5rem)_minmax(0,1fr)]"
          items={[
            { label: "Baseline", value: "The minutes before the deployment (30 by default)" },
            { label: "Comparison", value: "After a short warm-up, until 15 minutes after the deployment (defaults)" },
            { label: "Latency gate", value: "At least 25% and 20 ms slower, with a bootstrap confidence interval that excludes zero" },
            { label: "Error gate", value: "z-test and Fisher exact p < 0.01, at least +0.5 percentage points and +50% relative" },
            { label: "Minimum traffic", value: "Below the per-service request minimum, the verdict is “Insufficient data”" },
            { label: "Output", value: "Probable origin, blast radius and a severity score (not a probability)" },
          ]}
        />
      </div>
    </section>
  );
}

export function RiskSetup({ title = "No risk assessments yet" }: { title?: string }) {
  return (
    <EmptyState
      icon={Gauge}
      title={title}
      description="Add the ReleaseLens Action to a pull-request workflow. It computes change facts inside your runner (churn, files touched, test changes) and requests a CI failure risk score from 0 to 100, with the factors that raised and lowered it."
      action={
        <Button asChild size="sm">
          <Link href="/docs/github-setup">Read the setup guide</Link>
        </Button>
      }
    >
      <CodeBlock code={GITHUB_ACTION_YAML} title=".github/workflows/releaselens.yml" />
    </EmptyState>
  );
}

export function ServiceMapSetup() {
  return (
    <EmptyState
      icon={Network}
      title="No service graph yet"
      description="The map is drawn only from traces: every parent-to-child span that crosses a service boundary becomes an edge. Nothing is configured by hand, so the map appears once traces arrive for this environment."
      action={
        <Button asChild size="sm">
          <Link href="/app/services">Send traces</Link>
        </Button>
      }
      secondaryAction={
        <Button asChild size="sm" variant="ghost">
          <Link href="/docs/methodology">How origins are ranked</Link>
        </Button>
      }
    />
  );
}
