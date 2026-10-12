import { BookOpen, Rocket } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { CodeBlock } from "@/components/ui/code-block";
import { EmptyState } from "@/components/ui/empty-state";
import { KeyValueList } from "@/components/ui/key-value";
import { PageHeader } from "@/components/ui/page-header";
import { API_BASE_URL } from "@/lib/env";
import { deploymentCurl } from "@/lib/snippets";

export const metadata: Metadata = { title: "Deployments" };

export default function DeploymentsPage() {
  return (
    <>
      <PageHeader
        title="Deployments"
        description="Every release your CD pipeline reports, with its CI failure risk and the regression check that followed."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Deployments" }]}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs/deployment-api">
              <BookOpen aria-hidden="true" />
              Deployment API
            </Link>
          </Button>
        }
      />

      <EmptyState
        icon={Rocket}
        title="Send your first deployment event"
        description={
          <>
            ReleaseLens never deploys code. Your pipeline tells it when a release went out, and each event
            starts a before/after trace comparison for that service.
          </>
        }
        action={
          <Button asChild size="sm">
            <Link href="/docs/deployment-api">Read the guide</Link>
          </Button>
        }
        secondaryAction={
          <Button asChild size="sm" variant="ghost">
            <Link href="/app/services">Set up traces first</Link>
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
                    <code className="font-mono text-xs">x-releaselens-key</code> header with an API key that has
                    the <code className="font-mono text-xs">deployments:write</code> scope.
                  </>
                ),
                hint: "API keys are created in Settings once sign-in ships (M10).",
              },
              {
                label: "Response",
                value: (
                  <>
                    <code className="font-mono text-xs">202 Accepted</code> with an{" "}
                    <code className="font-mono text-xs">event_id</code>. Retrying the same event never creates a
                    duplicate.
                  </>
                ),
              },
              {
                label: "Availability",
                value: "The endpoint ships in M6. Field names may change until then.",
              },
            ]}
          />
        </div>
      </EmptyState>
    </>
  );
}
