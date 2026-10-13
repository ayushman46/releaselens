import { Crosshair, HelpCircle } from "lucide-react";

import { StatusPill } from "@/components/rl/status-pill";
import { CONFIDENCE_LABEL, type OriginView } from "@/lib/analysis";
import type { Tone } from "@/lib/tones";

const CONFIDENCE_TONE: Record<OriginView["confidence"], Tone> = { HIGH: "info", MEDIUM: "warning", LOW: "unknown" };

/**
 * Where the evidence points. Wording rule (shared with the API): this is a **probable origin**,
 * ranked from evidence in the traces, never a "root cause".
 */
export function OriginCard({ origin }: { origin: OriginView | null }) {
  return (
    <section aria-labelledby="origin-title" className="flex flex-col rounded-xl border bg-panel shadow-panel">
      <div className="flex flex-wrap items-center gap-2 border-b px-5 py-3.5">
        <Crosshair aria-hidden="true" className="size-4 text-muted-foreground" />
        <h2 id="origin-title" className="text-sm font-semibold">
          Probable origin
        </h2>
        {origin ? (
          <StatusPill tone={CONFIDENCE_TONE[origin.confidence]} label={CONFIDENCE_LABEL[origin.confidence]} size="sm" className="ml-auto" />
        ) : null}
      </div>
      <div className="flex-1 px-5 py-4">
        {!origin ? (
          <p className="text-sm text-muted-foreground">
            No probable origin: nothing regressed in this analysis, so there was nothing to rank.
          </p>
        ) : origin.service ? (
          <>
            <p className="text-2xl font-semibold tracking-tight text-foreground" data-testid="probable-origin">
              {origin.service}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              The service the evidence points to. A probable origin, not a proven cause: confirm it in the code and traces.
            </p>
          </>
        ) : (
          <div className="flex items-start gap-2 text-sm text-foreground-secondary">
            <HelpCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <p>
              No probable origin named: the evidence does not single out one service with enough confidence
              {origin.candidate ? (
                <>
                  . The top-ranked candidate is <span className="font-medium text-foreground">{origin.candidate.service}</span>
                </>
              ) : null}
              .
            </p>
          </div>
        )}

        {origin && origin.reasoning.length > 0 ? (
          <>
            <h3 className="mt-4 text-xs font-medium tracking-wide text-muted-foreground uppercase">Why</h3>
            <ul className="mt-2 space-y-1.5 text-sm text-foreground-secondary">
              {origin.reasoning.map((line) => (
                <li key={line} className="flex gap-2">
                  <span aria-hidden="true" className="mt-2 size-1 shrink-0 rounded-full bg-foreground-secondary" />
                  <span>{line}</span>
                </li>
              ))}
            </ul>
          </>
        ) : null}

        {origin?.summary ? <p className="mt-4 text-xs leading-5 text-muted-foreground">{origin.summary}</p> : null}

        {origin && origin.runnersUp.length > 0 ? (
          <p className="mt-3 text-xs text-muted-foreground">
            Other candidates:{" "}
            {origin.runnersUp.map((c, i) => (
              <span key={c.service}>
                {i > 0 ? ", " : ""}
                {c.service} ({c.score} pts)
              </span>
            ))}
            {origin.candidate ? ` vs ${origin.candidate.score} pts` : ""}
            {origin.margin !== null ? `, margin ${origin.margin}` : ""}.
          </p>
        ) : null}
      </div>
    </section>
  );
}
