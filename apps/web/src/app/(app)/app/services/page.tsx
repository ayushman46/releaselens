import { BookOpen, Boxes } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { API_BASE_URL } from "@/lib/env";
import { otelCollectorYaml, otelEnv } from "@/lib/snippets";

export const metadata: Metadata = { title: "Services" };

export default function ServicesPage() {
  return (
    <>
      <PageHeader
        title="Services"
        description="Every service that sends traces, with its latency, error rate and recent deployments."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Services" }]}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs/otel-setup">
              <BookOpen aria-hidden="true" />
              OpenTelemetry setup
            </Link>
          </Button>
        }
      />

      <EmptyState
        icon={Boxes}
        title="No services reporting"
        description={
          <>
            Services appear automatically when spans arrive at the OTLP endpoint, named by their{" "}
            <code className="font-mono text-xs text-foreground-secondary">service.name</code> resource attribute.
            Point your OpenTelemetry Collector, or your SDKs directly, at ReleaseLens.
          </>
        }
        action={
          <Button asChild size="sm">
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
        <p className="mt-3 text-xs leading-5 text-muted-foreground">
          Preview: OTLP ingest ships in M7. Keys need the <code className="font-mono">telemetry:write</code> scope.
        </p>
      </EmptyState>
    </>
  );
}
