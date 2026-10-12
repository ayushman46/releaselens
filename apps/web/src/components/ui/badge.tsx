import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

/**
 * A neutral label ("Preview", "M5", "Planned"). It carries no status meaning, so it never uses a
 * status colour: for health, risk or severity use StatusPill, RiskBadge or SeverityBadge.
 */
const badgeVariants = cva(
  "inline-flex h-5 shrink-0 items-center gap-1 rounded-[5px] border px-1.5 text-[11px] leading-none font-medium whitespace-nowrap [&_svg]:size-3 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        outline: "border-border bg-transparent text-foreground-secondary",
        subtle: "border-transparent bg-subtle text-foreground-secondary",
        brand: "border-brand/35 bg-brand/10 text-brand-text",
      },
    },
    defaultVariants: { variant: "outline" },
  },
);

export function Badge({
  className,
  variant,
  ...props
}: ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span data-slot="badge" className={cn(badgeVariants({ variant }), className)} {...props} />;
}
