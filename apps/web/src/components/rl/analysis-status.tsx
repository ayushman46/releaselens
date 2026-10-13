import { StatusPill } from "@/components/rl/status-pill";
import { Button } from "@/components/ui/button";
import { STATUS_META } from "@/lib/analysis";
import type { ServiceStatus } from "@/lib/api/types";

/** Regression verdict of an analysis or a service (icon + text, never colour alone). */
export function AnalysisStatusPill({
  status,
  size,
  prefix = "Status:",
  className,
}: {
  status: ServiceStatus | null | undefined;
  size?: "sm" | "md";
  prefix?: string;
  className?: string;
}) {
  if (!status) return <StatusPill tone="unknown" label="Not analysed" srPrefix={prefix} size={size} className={className} />;
  const meta = STATUS_META[status];
  return <StatusPill tone={meta.tone} label={meta.label} srPrefix={prefix} size={size} className={className} />;
}

/** "Load more" for cursor-paginated lists; says when the end is reached. */
export function LoadMore({
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
  shown,
  noun = "rows",
}: {
  hasNextPage: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
  shown: number;
  noun?: string;
}) {
  return (
    <div className="mt-3 flex items-center justify-between gap-3 text-xs text-muted-foreground">
      <span aria-live="polite">
        {shown.toLocaleString("en-US")} {noun} shown{hasNextPage ? "" : shown > 0 ? " · end of list" : ""}
      </span>
      {hasNextPage ? (
        <Button variant="outline" size="sm" onClick={fetchNextPage} disabled={isFetchingNextPage}>
          {isFetchingNextPage ? "Loading…" : "Load more"}
        </Button>
      ) : null}
    </div>
  );
}
