import { Flag, Rocket } from "lucide-react";

import { timelinePct, type TimelineModel } from "@/lib/analysis";
import { formatTime, minutesBetween } from "@/lib/format";
import { cn } from "@/lib/utils";

function minutes(from: number, to: number): string {
  const m = minutesBetween(new Date(from), new Date(to));
  return m === null ? "" : `${m} min`;
}

/**
 * baseline | deploy | warm-up | post window | onset, positioned from the analysis' resolved
 * windows. Segments differ by pattern and label as well as colour; the same facts are listed
 * below the bar as text.
 */
export function AnalysisTimeline({ model }: { model: TimelineModel }) {
  const seg = (from: number, to: number) => ({
    left: `${timelinePct(model, from)}%`,
    width: `${Math.max(0, timelinePct(model, to) - timelinePct(model, from))}%`,
  });
  const deployPct = timelinePct(model, model.deploy);
  const onsetPct = model.onset ? timelinePct(model, model.onset.at) : null;
  const truncated = model.post.to < model.plannedPostEnd;

  return (
    <figure aria-labelledby="timeline-caption" className="rounded-xl border bg-panel p-5 shadow-panel">
      <figcaption id="timeline-caption" className="text-sm font-semibold">
        Analysis windows
      </figcaption>
      <div aria-hidden="true" className="relative mt-10 mb-12 h-9">
        <div className="absolute inset-0 rounded-md bg-subtle" />
        <div
          className="absolute inset-y-0 rounded-l-md border border-info-border bg-info-bg [background-image:repeating-linear-gradient(135deg,transparent_0_6px,rgb(127_127_127/0.12)_6px_8px)]"
          style={seg(model.baseline.from, model.baseline.to)}
        />
        <div className="absolute inset-y-0 border-y border-dashed border-border-strong/60" style={seg(model.warmup.from, model.warmup.to)} />
        <div className="absolute inset-y-0 rounded-r-md border border-high-border bg-high-bg" style={seg(model.post.from, model.post.to)} />
        {truncated ? (
          <div className="absolute inset-y-0 rounded-r-md border border-dashed border-border-strong/50" style={seg(model.post.to, model.plannedPostEnd)} />
        ) : null}
        <span className="absolute top-1/2 -translate-y-1/2 pl-2 text-[11px] font-medium text-foreground-secondary" style={{ left: seg(model.baseline.from, model.baseline.to).left }}>
          Baseline
        </span>
        <span className="absolute top-1/2 -translate-y-1/2 pl-2 text-[11px] font-medium text-foreground-secondary" style={{ left: seg(model.post.from, model.post.to).left }}>
          After
        </span>
        <div className="absolute -top-8 bottom-0 flex -translate-x-1/2 flex-col items-center" style={{ left: `${deployPct}%` }}>
          <span className="flex items-center gap-1 rounded-full border bg-raised px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap text-foreground shadow-panel">
            <Rocket className="size-3" />
            Deploy
          </span>
          <span className="w-0.5 flex-1 bg-foreground" />
        </div>
        {onsetPct !== null ? (
          <div className="absolute top-0 -bottom-8 flex -translate-x-1/2 flex-col items-center" style={{ left: `${onsetPct}%` }}>
            <span className="w-0.5 flex-1 border-l-2 border-dotted border-critical" />
            <span className="flex items-center gap-1 rounded-full border border-critical-border bg-critical-bg px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap text-critical">
              <Flag className="size-3" />
              Onset
            </span>
          </div>
        ) : null}
      </div>
      <dl className={cn("grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2 lg:grid-cols-4")}>
        <div>
          <dt className="text-muted-foreground">Baseline</dt>
          <dd className="text-foreground">
            {formatTime(new Date(model.baseline.from))} – {formatTime(new Date(model.baseline.to))} ({minutes(model.baseline.from, model.baseline.to)})
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Deploy</dt>
          <dd className="text-foreground">{formatTime(new Date(model.deploy))}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Warm-up (excluded)</dt>
          <dd className="text-foreground">{minutes(model.warmup.from, model.warmup.to) || "none"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">After the deploy</dt>
          <dd className="text-foreground">
            {formatTime(new Date(model.post.from))} – {formatTime(new Date(model.post.to))} ({minutes(model.post.from, model.post.to)})
            {truncated ? `, of ${minutes(model.post.from, model.plannedPostEnd)} planned (early analysis)` : ""}
          </dd>
        </div>
        <div className="sm:col-span-2 lg:col-span-4">
          <dt className="text-muted-foreground">Estimated onset</dt>
          <dd className="text-foreground">
            {model.onset
              ? `${formatTime(new Date(model.onset.at))} in ${model.onset.service}${model.onset.minutesAfterDeploy !== null ? `, ${model.onset.minutesAfterDeploy} min after the deploy` : ""}: the first minute whose p95 exceeded the baseline's confidence interval`
              : "None estimated (computed only when a p95 latency regression passed every gate)"}
          </dd>
        </div>
      </dl>
    </figure>
  );
}
