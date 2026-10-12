import { ArrowRight } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Callout } from "@/components/ui/callout";
import { PageHeader } from "@/components/ui/page-header";
import { DOCS } from "@/lib/docs";

export const metadata: Metadata = {
  title: "Docs",
  description: "ReleaseLens documentation: quick start, integrations, methodology and limitations.",
};

export default function DocsIndexPage() {
  return (
    <>
      <PageHeader
        title="Documentation"
        description="Everything needed to connect ReleaseLens and to judge what its numbers mean. Pages are being written alongside the features they document."
      />

      <Callout tone="info" title="Docs in progress" className="mb-8">
        Each page below lists what it will cover and the milestone that ships the feature. Snippets on these
        pages are contract previews and can change until that milestone lands.
      </Callout>

      <ul className="grid gap-3 sm:grid-cols-2">
        {DOCS.map((doc) => {
          const Icon = doc.icon;
          return (
            <li key={doc.slug}>
              <Link
                href={`/docs/${doc.slug}`}
                className="group flex h-full flex-col rounded-xl border bg-panel p-5 shadow-panel transition-colors hover:border-border-strong/60"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="flex size-8 items-center justify-center rounded-lg border bg-subtle">
                    <Icon aria-hidden="true" className="size-4 text-brand-text" />
                  </span>
                  <Badge>Planned · {doc.milestone}</Badge>
                </div>
                <h2 className="mt-4 text-sm font-semibold">{doc.title}</h2>
                <p className="mt-1 flex-1 text-sm leading-6 text-muted-foreground">{doc.summary}</p>
                <span className="mt-4 inline-flex items-center gap-1 text-xs font-medium text-brand-text">
                  See the outline
                  <ArrowRight aria-hidden="true" className="size-3 transition-transform group-hover:translate-x-0.5" />
                </span>
              </Link>
            </li>
          );
        })}
      </ul>
    </>
  );
}
