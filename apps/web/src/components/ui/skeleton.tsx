import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

/** Decorative loading placeholder. Pair it with a visible or sr-only "Loading…" status. */
export function Skeleton({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      data-slot="skeleton"
      aria-hidden="true"
      className={cn("animate-pulse rounded-md bg-subtle motion-reduce:animate-none", className)}
      {...props}
    />
  );
}

/** A labelled loading region: skeleton shapes for sighted users, one status message for AT. */
export function LoadingState({
  label = "Loading…",
  lines = 3,
  className,
}: {
  label?: string;
  lines?: number;
  className?: string;
}) {
  return (
    <div role="status" className={cn("space-y-2.5", className)}>
      <span className="sr-only">{label}</span>
      {Array.from({ length: lines }, (_, i) => (
        <Skeleton key={i} className={cn("h-4", i === lines - 1 ? "w-2/3" : "w-full")} />
      ))}
    </div>
  );
}
