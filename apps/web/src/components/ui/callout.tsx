import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { TONE_CLASSES, TONE_META, type Tone } from "@/lib/tones";
import { cn } from "@/lib/utils";

/** A highlighted note. The tone icon is always shown next to the title or body. */
export function Callout({
  tone = "info",
  title,
  icon,
  children,
  className,
}: {
  tone?: Tone;
  title?: string;
  icon?: LucideIcon;
  children?: ReactNode;
  className?: string;
}) {
  const Icon = icon ?? TONE_META[tone].icon;
  return (
    <div
      role="note"
      data-tone={tone}
      className={cn(
        "flex gap-3 rounded-lg border px-4 py-3 text-sm",
        TONE_CLASSES[tone].surface,
        className,
      )}
    >
      <Icon aria-hidden="true" className={cn("mt-0.5 size-4 shrink-0", TONE_CLASSES[tone].text)} />
      <div className="min-w-0 space-y-1">
        {title ? <p className="font-medium text-foreground">{title}</p> : null}
        {children ? <div className="leading-6 text-foreground-secondary">{children}</div> : null}
      </div>
    </div>
  );
}
