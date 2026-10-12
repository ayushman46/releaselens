import { BrainCircuit } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { StatCard } from "@/components/rl/stat-card";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { KeyValueList } from "@/components/ui/key-value";
import { PageHeader } from "@/components/ui/page-header";

export const metadata: Metadata = { title: "Model" };

const MEASURED_IN_M4 = "Not measured yet. Published with the model card when M4 (training and evaluation) completes.";

export default function ModelPage() {
  return (
    <>
      <PageHeader
        title="Model"
        description="The CI failure risk model behind every score: what it predicts, what it learned from and how well it does."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Model" }]}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs/limitations">Limitations</Link>
          </Button>
        }
      />

      <section aria-label="Evaluation" className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="PR-AUC, chronological split" missingReason={MEASURED_IN_M4} />
        <StatCard label="PR-AUC, unseen projects" missingReason={MEASURED_IN_M4} />
        <StatCard label="Calibration (Brier score)" missingReason={MEASURED_IN_M4} />
        <StatCard
          label="Training builds"
          missingReason="The exact count after de-duplication is reported by the M2 ingestion run."
        />
      </section>

      <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <section aria-labelledby="facts-title" className="rounded-xl border bg-panel shadow-panel">
          <h2 id="facts-title" className="border-b px-5 py-3.5 text-sm font-semibold">
            What the model is
          </h2>
          <div className="px-5">
            <KeyValueList
              labelWidth="sm:grid-cols-[minmax(0,8.5rem)_minmax(0,1fr)]"
              items={[
                {
                  label: "Predicts",
                  value: "Whether a change fails CI: the build fails or errors. Canceled builds are excluded.",
                },
                {
                  label: "Output",
                  value: "A score from 0 to 100 with a band (Low, Medium, High, Critical) and SHAP explanations.",
                  hint: "A score, not a probability, unless calibration testing justifies calling it one.",
                },
                {
                  label: "Training data",
                  value: "TravisTorrent, Java subset: about 200,000 CI builds from open-source projects, 2012–2016.",
                  hint: "Beller, Gousios & Zaidman, MSR 2017. CC BY 4.0.",
                },
                {
                  label: "Never an input",
                  value: "CI pass/fail and CI duration. They appear next to the score, never inside it, because they would leak the answer.",
                },
                {
                  label: "Evaluation",
                  value: "Chronological, cross-project and cold-start splits against simple baselines (M4).",
                },
              ]}
            />
          </div>
        </section>

        <EmptyState
          icon={BrainCircuit}
          title="No model registered yet"
          description="Training, evaluation and the model card arrive in M4. Until a model is promoted, no metrics are shown here, and the numbers above stay empty rather than estimated."
          action={
            <Button asChild size="sm" variant="outline">
              <Link href="/docs/methodology">Read the methodology</Link>
            </Button>
          }
          headingLevel={2}
          className="self-start"
        />
      </div>
    </>
  );
}
