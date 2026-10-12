import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * What a screen shows before there is data: what will appear here, and how to make it appear.
 * Never pad an empty screen with sample rows; explain the next step instead.
 */
export function EmptyState({
  icon: Icon,
  title,
  description,
  action,
  secondaryAction,
  children,
  headingLevel = 2,
  variant = "panel",
  className,
}: {
  icon: LucideIcon;
  title: string;
  /** Guidance: what appears here and what makes it appear. */
  description: ReactNode;
  /** Primary next step (usually a Button). */
  action?: ReactNode;
  secondaryAction?: ReactNode;
  /** Extra content under the actions, e.g. a CodeBlock with the setup snippet. */
  children?: ReactNode;
  headingLevel?: 2 | 3;
  /** `panel` draws its own dashed frame; `bare` is for use inside an existing card or table. */
  variant?: "panel" | "bare";
  className?: string;
}) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  return (
    <section
      data-slot="empty-state"
      className={cn(
        "flex flex-col items-center px-4 text-center sm:px-6",
        variant === "panel" ? "rounded-xl border border-dashed bg-panel/40 py-12 sm:py-14" : "py-6",
        className,
      )}
    >
      <div className="relative mb-5">
        <div
          aria-hidden="true"
          className="absolute -inset-3 rounded-2xl bg-gradient-to-b from-subtle to-transparent opacity-70"
        />
        <div className="relative flex size-11 items-center justify-center rounded-xl border bg-raised shadow-panel">
          <Icon aria-hidden="true" className="size-5 text-foreground-secondary" />
        </div>
      </div>
      <Heading className="text-base font-semibold tracking-tight text-foreground">{title}</Heading>
      <div className="mt-2 max-w-lg text-sm leading-6 text-pretty text-muted-foreground">{description}</div>
      {action || secondaryAction ? (
        <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
          {action}
          {secondaryAction}
        </div>
      ) : null}
      {children ? <div className="mt-8 w-full max-w-2xl text-left">{children}</div> : null}
    </section>
  );
}
