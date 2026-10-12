import { BookOpen, Gauge } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { CodeBlock } from "@/components/ui/code-block";
import { EmptyState } from "@/components/ui/empty-state";
import { PageHeader } from "@/components/ui/page-header";
import { GITHUB_ACTION_YAML } from "@/lib/snippets";

export const metadata: Metadata = { title: "Risk analysis" };

export default function RiskAnalysisPage() {
  return (
    <>
      <PageHeader
        title="Risk analysis"
        description="CI failure risk for each pull request, scored before merge, with the factors behind every score."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Risk analysis" }]}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs/github-setup">
              <BookOpen aria-hidden="true" />
              GitHub setup
            </Link>
          </Button>
        }
      />

      <Callout tone="info" title="What the score means" className="mb-6">
        It estimates how likely a change is to fail CI, based on historical builds. It is not the chance of an
        outage, and it is shown as a 0–100 score, not a probability.{" "}
        <Link href="/docs/limitations" className="font-medium text-foreground underline underline-offset-2">
          Read the limitations
        </Link>
      </Callout>

      <EmptyState
        icon={Gauge}
        title="No risk assessments yet"
        description="Add the ReleaseLens Action to a pull-request workflow. It computes change facts inside your runner (churn, files touched, test changes) and requests a CI failure risk score from 0 to 100, with the factors that raised and lowered it. The GitHub App posts the result as a Check Run."
        action={
          <Button asChild size="sm">
            <Link href="/docs/github-setup">Read the setup guide</Link>
          </Button>
        }
      >
        <CodeBlock code={GITHUB_ACTION_YAML} title=".github/workflows/releaselens.yml" />
        <p className="mt-3 text-xs leading-5 text-muted-foreground">
          Preview: the Action and the GitHub App ship in M5. The workflow above shows the planned shape.
        </p>
      </EmptyState>
    </>
  );
}
