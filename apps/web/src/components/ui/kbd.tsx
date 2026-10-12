"use client";

import { useSyncExternalStore, type ComponentProps } from "react";

import { cn } from "@/lib/utils";

export function Kbd({ className, ...props }: ComponentProps<"kbd">) {
  return (
    <kbd
      data-slot="kbd"
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-[4px] border border-border bg-subtle px-1 font-sans text-[11px] leading-none font-medium text-foreground-secondary",
        className,
      )}
      {...props}
    />
  );
}

const noop = () => () => {};

/** True on Apple platforms (client only; the server snapshot assumes "not Mac"). */
export function useIsMac(): boolean {
  return useSyncExternalStore(
    noop,
    () => /mac|iphone|ipad|ipod/i.test(navigator.userAgent),
    () => false,
  );
}

/** Renders ⌘K on Apple platforms and Ctrl K elsewhere, with a spoken label. */
export function ModKey({ letter, className }: { letter: string; className?: string }) {
  const isMac = useIsMac();
  const label = `${isMac ? "Command" : "Control"} ${letter}`;
  return (
    <span className={cn("inline-flex items-center gap-0.5", className)}>
      <span className="sr-only">{label}</span>
      <Kbd aria-hidden="true">{isMac ? "⌘" : "Ctrl"}</Kbd>
      <Kbd aria-hidden="true">{letter}</Kbd>
    </span>
  );
}
