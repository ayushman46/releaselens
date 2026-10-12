import type { Metadata } from "next";

import { Badge } from "@/components/ui/badge";

export const metadata: Metadata = {
  title: "Changelog",
  description: "What shipped in ReleaseLens, milestone by milestone.",
};

const ENTRIES = [
  {
    id: "m1",
    date: "2026-10-13",
    milestone: "M1",
    title: "Foundation",
    status: "Closing: the clean-clone check of the full local stack is still pending.",
    summary:
      "The groundwork every later milestone builds on. Nothing customer-facing yet: no risk scores, no regression detection.",
    items: [
      "Python workspace with a single lockfile for every backend component.",
      "API skeleton with liveness and readiness probes, structured JSON logs carrying request and trace IDs, and the baseline database migration (organizations, users, memberships, API keys).",
      "Worker skeleton with graceful shutdown and a health endpoint.",
      "Local stack in Docker Compose (PostgreSQL, Kafka, migrations, API, worker, web, mail sink), each service with a health check and a memory limit.",
      "Web app skeleton and a live system status page that probes the API from your browser.",
      "CI workflows for the backend, the frontend, the data pipeline and security scans.",
    ],
  },
];

export default function ChangelogPage() {
  return (
    <div className="mx-auto max-w-3xl px-4 py-16 sm:px-6 sm:py-20">
      <p className="text-xs font-medium tracking-wide text-brand-text uppercase">Changelog</p>
      <h1 className="mt-2 text-3xl font-semibold tracking-tight sm:text-4xl">What shipped</h1>
      <p className="mt-3 text-base leading-7 text-muted-foreground">
        ReleaseLens is built in milestones. Each entry says what landed and what is still open.
      </p>

      <ol className="mt-12 space-y-12 border-l pl-6 sm:pl-8">
        {ENTRIES.map((entry) => (
          <li key={entry.id} className="relative">
            <span
              aria-hidden="true"
              className="absolute top-1.5 -left-[29px] size-2.5 rounded-full border-2 border-background bg-border-strong sm:-left-[37px]"
            />
            <article aria-labelledby={`${entry.id}-title`}>
              <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                <time dateTime={entry.date}>
                  {new Date(`${entry.date}T00:00:00Z`).toLocaleDateString("en-US", {
                    year: "numeric",
                    month: "long",
                    day: "numeric",
                    timeZone: "UTC",
                  })}
                </time>
                <Badge>{entry.milestone}</Badge>
              </div>
              <h2 id={`${entry.id}-title`} className="mt-2 text-xl font-semibold tracking-tight">
                {entry.title}
              </h2>
              <p className="mt-2 text-sm leading-6 text-foreground-secondary">{entry.summary}</p>
              <p className="mt-2 text-xs leading-5 text-muted-foreground">
                <span className="font-medium text-foreground-secondary">Status:</span> {entry.status}
              </p>
              <ul className="mt-4 list-disc space-y-2 pl-5 text-sm leading-6 text-foreground-secondary marker:text-muted-foreground">
                {entry.items.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </article>
          </li>
        ))}
      </ol>
    </div>
  );
}
