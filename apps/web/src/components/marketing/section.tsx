import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function Section({
  id,
  eyebrow,
  title,
  description,
  children,
  className,
  bordered = true,
}: {
  id: string;
  eyebrow?: string;
  title: string;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
  bordered?: boolean;
}) {
  const headingId = `${id}-title`;
  return (
    <section id={id} aria-labelledby={headingId} className={cn(bordered && "border-t", className)}>
      <div className="mx-auto max-w-6xl px-4 py-16 sm:px-6 sm:py-24">
        <div className="max-w-2xl">
          {eyebrow ? (
            <p className="text-xs font-medium tracking-wide text-brand-text uppercase">{eyebrow}</p>
          ) : null}
          <h2 id={headingId} className="mt-2 text-2xl font-semibold tracking-tight text-balance sm:text-3xl">
            {title}
          </h2>
          {description ? (
            <p className="mt-3 text-base leading-7 text-pretty text-muted-foreground">{description}</p>
          ) : null}
        </div>
        <div className="mt-10 sm:mt-12">{children}</div>
      </div>
    </section>
  );
}
