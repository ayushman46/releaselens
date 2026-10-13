"use client";

import { Info } from "lucide-react";
import dynamic from "next/dynamic";

import { Callout } from "@/components/ui/callout";
import { Skeleton } from "@/components/ui/skeleton";
import { headlineText, type ReportView } from "@/lib/findings";
import { formatCount, formatDate } from "@/lib/format";

const RateChart = dynamic(() => import("@/components/charts/rate-chart").then((m) => m.RateChart), {
  ssr: false,
  loading: () => <Skeleton className="h-60 rounded-lg" />,
});

const CORRELATION = /not causation/i;

export function AnalyticsView({ report }: { report: ReportView }) {
  return (
    <>
      <Callout tone="info" title="Associations, not causes" className="mb-6">
        These findings come from the historical study the risk model learned from: {report.dataset.source}. They describe
        correlations in observational data; project, period, author and change type are not held fixed, so none of them shows that
        one thing causes another. With this many builds even negligible differences are &ldquo;significant&rdquo;: read the effect size
        and its interval, not the p-value.
      </Callout>

      <section aria-label="Study population" className="mb-6 grid gap-3 sm:grid-cols-3">
        <div className="rounded-xl border bg-panel p-4 shadow-panel">
          <p className="text-xs text-muted-foreground">Labelled builds</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums">{formatCount(report.population.labelled)}</p>
          <p className="mt-1 text-xs text-muted-foreground">{report.population.labelRule}</p>
        </div>
        <div className="rounded-xl border bg-panel p-4 shadow-panel">
          <p className="text-xs text-muted-foreground">Projects</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums">{formatCount(report.population.projects)}</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {formatDate(report.dataset.first)} – {formatDate(report.dataset.last)} · {report.dataset.subset}
          </p>
        </div>
        <div className="rounded-xl border bg-panel p-4 shadow-panel">
          <p className="text-xs text-muted-foreground">Overall failure rate</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums">{(report.population.rate * 100).toFixed(2)}%</p>
          <p className="mt-1 text-xs text-muted-foreground">
            95% CI {(report.population.low * 100).toFixed(2)}–{(report.population.high * 100).toFixed(2)}%
          </p>
        </div>
      </section>

      <div className="space-y-6">
        {report.findings.map((f) => (
          <article key={f.key} aria-labelledby={`finding-${f.key}`} className="rounded-xl border bg-panel shadow-panel">
            <header className="border-b px-5 py-4">
              <h2 id={`finding-${f.key}`} className="text-base font-semibold tracking-tight">
                {f.title}
              </h2>
              <p className="mt-0.5 text-sm text-muted-foreground">{f.question}</p>
              <p className="mt-2 inline-flex flex-wrap items-center gap-x-2 rounded-md bg-subtle px-2 py-1 text-xs text-foreground-secondary">
                <span className="font-medium text-foreground">{headlineText(f.headline)}</span>
                <span>· n = {formatCount(f.populationN)}</span>
              </p>
            </header>
            <div className="grid gap-6 px-5 py-4 lg:grid-cols-2">
              {f.charts.map((chart) => (
                <RateChart key={chart.id} spec={chart} />
              ))}
            </div>
            <div className="space-y-3 border-t px-5 py-4 text-sm">
              <p className="leading-6 text-foreground-secondary">{f.summary}</p>
              <ul className="space-y-1.5 text-xs leading-5 text-muted-foreground">
                {f.caveats.map((c) => (
                  <li key={c} className="flex gap-2">
                    <Info aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                    <span className={CORRELATION.test(c) ? "font-medium text-foreground-secondary" : undefined}>{c}</span>
                  </li>
                ))}
              </ul>
            </div>
          </article>
        ))}
      </div>

      <section aria-labelledby="report-caveats" className="mt-6 rounded-xl border bg-panel px-5 py-4 shadow-panel">
        <h2 id="report-caveats" className="text-sm font-semibold">
          Report-wide caveats
        </h2>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
          {report.caveats.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
        <p className="mt-3 text-xs text-muted-foreground">
          Run <code className="font-mono">{report.runId}</code>, report schema {report.schemaVersion}. Source: docs/analytics/findings.json.
        </p>
      </section>
    </>
  );
}
