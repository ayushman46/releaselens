"use client";

import { CircleX, RefreshCw } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

function messageOf(error: unknown): string | undefined {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === "string" && error) return error;
  return undefined;
}

/** A failure the user can act on: what went wrong, the technical detail, and a retry. */
export function ErrorState({
  title = "Something went wrong",
  description = "This part of the page could not be loaded.",
  error,
  onRetry,
  retrying = false,
  className,
}: {
  title?: string;
  description?: ReactNode;
  error?: unknown;
  onRetry?: () => void;
  retrying?: boolean;
  className?: string;
}) {
  const detail = messageOf(error);
  return (
    <section
      role="alert"
      data-slot="error-state"
      className={cn(
        "flex flex-col items-center rounded-xl border bg-panel px-6 py-12 text-center",
        className,
      )}
    >
      <div className="mb-4 flex size-11 items-center justify-center rounded-xl border border-critical-border bg-critical-bg">
        <CircleX aria-hidden="true" className="size-5 text-critical" />
      </div>
      <h2 className="text-base font-semibold tracking-tight">{title}</h2>
      <div className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">{description}</div>
      {detail ? (
        <p className="mt-3 max-w-md rounded-md border bg-subtle px-2.5 py-1.5 font-mono text-xs break-words text-foreground-secondary">
          {detail}
        </p>
      ) : null}
      {onRetry ? (
        <Button variant="outline" size="sm" className="mt-6" onClick={onRetry} disabled={retrying}>
          <RefreshCw aria-hidden="true" className={cn(retrying && "animate-spin motion-reduce:animate-none")} />
          {retrying ? "Retrying…" : "Try again"}
        </Button>
      ) : null}
    </section>
  );
}
