"use client";

import {
  createContext,
  useContext,
  useId,
  useState,
  type ComponentProps,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { cn } from "@/lib/utils";

interface TabsContextValue {
  value: string;
  select: (value: string) => void;
  idFor: (kind: "tab" | "panel", value: string) => string;
}

const TabsContext = createContext<TabsContextValue | null>(null);

function useTabs(): TabsContextValue {
  const ctx = useContext(TabsContext);
  if (!ctx) throw new Error("Tabs components must be used inside <Tabs>");
  return ctx;
}

/**
 * WAI-ARIA tabs with automatic activation: arrow keys move focus and select, Home/End jump to the
 * first/last tab, only the selected tab is in the Tab order (roving tabindex), and every panel is
 * labelled by its tab. Inactive panels stay in the DOM (`hidden`) so their content is server-rendered.
 */
export function Tabs({
  defaultValue,
  value: controlled,
  onValueChange,
  children,
  className,
}: {
  defaultValue: string;
  value?: string;
  onValueChange?: (value: string) => void;
  children: ReactNode;
  className?: string;
}) {
  const baseId = useId();
  const [uncontrolled, setUncontrolled] = useState(defaultValue);
  const value = controlled ?? uncontrolled;
  const select = (next: string) => {
    if (controlled === undefined) setUncontrolled(next);
    onValueChange?.(next);
  };
  const idFor = (kind: "tab" | "panel", v: string) => `${baseId}-${kind}-${v}`;

  return (
    <TabsContext.Provider value={{ value, select, idFor }}>
      <div data-slot="tabs" className={className}>
        {children}
      </div>
    </TabsContext.Provider>
  );
}

export function TabsList({
  className,
  children,
  "aria-label": ariaLabel,
}: {
  className?: string;
  children: ReactNode;
  "aria-label": string;
}) {
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const tabs = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]:not([disabled])'),
    );
    const index = tabs.indexOf(document.activeElement as HTMLButtonElement);
    if (index === -1) return;
    const target =
      event.key === "ArrowRight"
        ? tabs[(index + 1) % tabs.length]
        : event.key === "ArrowLeft"
          ? tabs[(index - 1 + tabs.length) % tabs.length]
          : event.key === "Home"
            ? tabs[0]
            : event.key === "End"
              ? tabs[tabs.length - 1]
              : undefined;
    if (!target) return;
    event.preventDefault();
    target.focus();
    target.click();
  };

  return (
    <div
      role="tablist"
      aria-label={ariaLabel}
      onKeyDown={onKeyDown}
      className={cn(
        "inline-flex max-w-full items-center gap-1 overflow-x-auto border-b [scrollbar-width:none]",
        className,
      )}
    >
      {children}
    </div>
  );
}

export function TabsTrigger({
  value,
  className,
  children,
  ...props
}: Omit<ComponentProps<"button">, "value" | "onClick" | "role"> & { value: string }) {
  const tabs = useTabs();
  const selected = tabs.value === value;
  return (
    <button
      type="button"
      role="tab"
      id={tabs.idFor("tab", value)}
      aria-selected={selected}
      aria-controls={tabs.idFor("panel", value)}
      tabIndex={selected ? 0 : -1}
      data-state={selected ? "active" : "inactive"}
      onClick={() => tabs.select(value)}
      className={cn(
        "relative -mb-px inline-flex h-9 shrink-0 items-center gap-1.5 rounded-t-md border-b-2 px-3 text-sm font-medium whitespace-nowrap transition-colors [&_svg]:size-4 [&_svg]:shrink-0",
        selected
          ? "border-brand text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground",
        className,
      )}
      {...props}
    >
      {children}
    </button>
  );
}

export function TabsContent({
  value,
  className,
  children,
}: {
  value: string;
  className?: string;
  children: ReactNode;
}) {
  const tabs = useTabs();
  const selected = tabs.value === value;
  return (
    <div
      role="tabpanel"
      id={tabs.idFor("panel", value)}
      aria-labelledby={tabs.idFor("tab", value)}
      hidden={!selected}
      tabIndex={0}
      className={cn("pt-4", className)}
    >
      {children}
    </div>
  );
}
