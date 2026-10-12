import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface KeyValueItem {
  label: string;
  value: ReactNode;
  /** Optional small print under the value. */
  hint?: ReactNode;
}

/** A description list: label on the left, value on the right (stacked on narrow screens). */
export function KeyValueList({
  items,
  className,
  labelWidth = "sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)]",
}: {
  items: KeyValueItem[];
  className?: string;
  /** Tailwind grid template for the label/value columns from `sm` up. */
  labelWidth?: string;
}) {
  return (
    <dl data-slot="key-value" className={cn("divide-y text-sm", className)}>
      {items.map((item) => (
        <div key={item.label} className={cn("grid gap-1 py-3 sm:gap-6", labelWidth)}>
          <dt className="text-muted-foreground">{item.label}</dt>
          <dd className="min-w-0 text-foreground">
            {item.value}
            {item.hint ? <p className="mt-1 text-xs leading-5 text-muted-foreground">{item.hint}</p> : null}
          </dd>
        </div>
      ))}
    </dl>
  );
}
