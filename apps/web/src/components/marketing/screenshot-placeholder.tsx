import { ImageOff } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * Stands in for a product screenshot until real ones exist. It says so in words, and contains no
 * mock UI or numbers that could be mistaken for real product output.
 */
export function ScreenshotPlaceholder({
  label,
  caption,
  className,
}: {
  /** What the real screenshot will show, e.g. "Regression detail". */
  label: string;
  caption?: string;
  className?: string;
}) {
  return (
    <figure className={cn("overflow-hidden rounded-xl border bg-panel shadow-panel", className)}>
      <div aria-hidden="true" className="flex h-9 items-center gap-1.5 border-b bg-subtle/50 px-3">
        <span className="size-2.5 rounded-full border border-border-strong/50" />
        <span className="size-2.5 rounded-full border border-border-strong/50" />
        <span className="size-2.5 rounded-full border border-border-strong/50" />
      </div>
      <div
        role="img"
        aria-label={`Placeholder for a product screenshot: ${label}. No real screenshot exists yet.`}
        className="relative flex aspect-[16/9] max-h-[26rem] w-full flex-col items-center justify-center gap-3 p-6 text-center"
        style={{
          backgroundImage:
            "linear-gradient(color-mix(in oklab, var(--border) 60%, transparent) 1px, transparent 1px), linear-gradient(90deg, color-mix(in oklab, var(--border) 60%, transparent) 1px, transparent 1px)",
          backgroundSize: "40px 40px",
          backgroundPosition: "-1px -1px",
        }}
      >
        <div className="flex flex-col items-center gap-3 rounded-xl border bg-panel/95 px-6 py-5">
          <ImageOff aria-hidden="true" className="size-5 text-muted-foreground" />
          <div>
            <p className="text-sm font-medium text-foreground">Screenshot placeholder</p>
            <p className="mt-0.5 text-xs text-muted-foreground">{label}</p>
          </div>
        </div>
      </div>
      {caption ? (
        <figcaption className="border-t px-4 py-2.5 text-xs text-muted-foreground">{caption}</figcaption>
      ) : null}
    </figure>
  );
}
