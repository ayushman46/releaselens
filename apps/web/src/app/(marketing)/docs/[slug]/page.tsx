import { ArrowLeft, Circle } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { Callout } from "@/components/ui/callout";
import { CodeBlock } from "@/components/ui/code-block";
import { PageHeader } from "@/components/ui/page-header";
import { DOCS, getDoc } from "@/lib/docs";
import { deploymentCurl, GITHUB_ACTION_YAML, otelCollectorYaml, otelEnv } from "@/lib/snippets";

export const dynamicParams = false;

export function generateStaticParams() {
  return DOCS.map((doc) => ({ slug: doc.slug }));
}

type Props = { params: Promise<{ slug: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const doc = getDoc((await params).slug);
  return doc ? { title: doc.title, description: doc.summary } : {};
}

/** Snippets that already exist as contract previews, per page. */
const PREVIEWS: Record<string, ReactNode> = {
  "github-setup": <CodeBlock code={GITHUB_ACTION_YAML} title=".github/workflows/releaselens.yml" />,
  "deployment-api": <CodeBlock code={deploymentCurl()} title="deploy.sh" />,
  "otel-setup": (
    <div className="space-y-4">
      <CodeBlock code={otelCollectorYaml()} title="otel-collector.yaml" />
      <CodeBlock code={otelEnv()} title="SDK environment variables" />
    </div>
  ),
};

export default async function DocPage({ params }: Props) {
  const doc = getDoc((await params).slug);
  if (!doc) notFound();
  const preview = PREVIEWS[doc.slug];

  return (
    <article>
      <PageHeader
        title={doc.title}
        description={doc.summary}
        breadcrumbs={[{ label: "Docs", href: "/docs" }, { label: doc.title }]}
        meta={<Badge>Planned · {doc.milestone}</Badge>}
      />

      <Callout tone="info" title="This page is being written">
        The feature it documents ships in {doc.milestone}. Until then, here is the outline of what the page
        will cover.
      </Callout>

      <section aria-labelledby="covers" className="mt-10">
        <h2 id="covers" className="text-base font-semibold tracking-tight">
          What this page will cover
        </h2>
        <ul className="mt-4 space-y-2.5 text-sm leading-6">
          {doc.covers.map((line) => (
            <li key={line} className="flex gap-3 text-foreground-secondary">
              <Circle aria-hidden="true" className="mt-2 size-1.5 shrink-0 fill-current text-muted-foreground" />
              {line}
            </li>
          ))}
        </ul>
      </section>

      {preview ? (
        <section aria-labelledby="preview" className="mt-10">
          <h2 id="preview" className="text-base font-semibold tracking-tight">
            Contract preview
          </h2>
          <p className="mt-1 mb-4 text-sm text-muted-foreground">
            From the architecture docs. Field names can change until {doc.milestone}.
          </p>
          {preview}
        </section>
      ) : null}

      <div className="mt-12 border-t pt-6">
        <Link href="/docs" className="inline-flex items-center gap-1.5 rounded-sm text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft aria-hidden="true" className="size-3.5" />
          All docs
        </Link>
      </div>
    </article>
  );
}
