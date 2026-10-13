import type { Metadata } from "next";
import Link from "next/link";

import { ModelOverview, type ModelCardExcerpts } from "@/components/model/model-view";
import { Button } from "@/components/ui/button";
import { PageHeader } from "@/components/ui/page-header";
import erroredExcluded from "@/data/ml/errored_excluded.json";
import primary from "@/data/ml/failed_or_errored.json";
import card from "@/data/ml/model-card.json";
import { buildModelView } from "@/lib/model-results";

export const metadata: Metadata = { title: "Model" };

// docs/ml/results/*.json and the model card, copied by `pnpm sync:data` (global reference data).
const model = buildModelView(primary as unknown as Record<string, unknown>);
const sensitivity = buildModelView(erroredExcluded as unknown as Record<string, unknown>);

export default function ModelPage() {
  return (
    <>
      <PageHeader
        title="Model"
        description="The CI failure risk model behind every score: what it learned from, how well it does on unseen data and where it falls short."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Model" }]}
        actions={
          <Button asChild variant="outline" size="sm">
            <Link href="/docs/limitations">Limitations</Link>
          </Button>
        }
      />
      <ModelOverview model={model} sensitivity={sensitivity} card={card as ModelCardExcerpts} />
    </>
  );
}
