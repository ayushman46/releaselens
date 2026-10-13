"use client";

import { Check } from "lucide-react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";

import { cn } from "@/lib/utils";

interface MenuContextValue {
  close: (restoreFocus?: boolean) => void;
}

const MenuContext = createContext<MenuContextValue | null>(null);

function items(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>('[role^="menuitem"]:not([aria-disabled="true"])'));
}

/**
 * WAI-ARIA menu button: Enter/Space/↓ open on the first item, ↑ on the last; ↑/↓ move,
 * Home/End jump, Escape or Tab closes (Escape returns focus to the button), a click outside closes.
 */
export function Menu({
  trigger,
  label,
  children,
  align = "start",
  className,
}: {
  /** Render prop for the button so callers control its look. */
  trigger: (props: {
    "aria-haspopup": "menu";
    "aria-expanded": boolean;
    "aria-controls": string;
    onClick: () => void;
    onKeyDown: (e: KeyboardEvent<HTMLButtonElement>) => void;
  }) => ReactNode;
  label: string;
  children: ReactNode;
  align?: "start" | "end";
  className?: string;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [focusTarget, setFocusTarget] = useState<"first" | "last">("first");

  const close = useCallback((restoreFocus = true) => {
    setOpen(false);
    if (restoreFocus) wrapperRef.current?.querySelector<HTMLButtonElement>("button[aria-haspopup]")?.focus();
  }, []);

  useEffect(() => {
    if (!open) return;
    const list = items(listRef.current);
    (focusTarget === "first" ? list[0] : list[list.length - 1])?.focus();
    const onPointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!wrapperRef.current?.contains(target)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    return () => document.removeEventListener("pointerdown", onPointer);
  }, [open, focusTarget]);

  const onButtonKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      setFocusTarget(event.key === "ArrowDown" ? "first" : "last");
      setOpen(true);
    }
  };

  const onListKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const list = items(listRef.current);
    const index = list.indexOf(document.activeElement as HTMLElement);
    let next: HTMLElement | undefined;
    if (event.key === "ArrowDown") next = list[(index + 1) % list.length];
    else if (event.key === "ArrowUp") next = list[(index - 1 + list.length) % list.length];
    else if (event.key === "Home") next = list[0];
    else if (event.key === "End") next = list[list.length - 1];
    else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close(true);
      return;
    } else if (event.key === "Tab") {
      close(false);
      return;
    }
    if (next) {
      event.preventDefault();
      next.focus();
    }
  };

  return (
    <MenuContext.Provider value={{ close }}>
      <div ref={wrapperRef} className={cn("relative", className)}>
        {trigger({
          "aria-haspopup": "menu",
          "aria-expanded": open,
          "aria-controls": id,
          onClick: () => {
            setFocusTarget("first");
            setOpen((v) => !v);
          },
          onKeyDown: onButtonKeyDown,
        })}
        <div
          ref={listRef}
          id={id}
          role="menu"
          aria-label={label}
          hidden={!open}
          onKeyDown={onListKeyDown}
          className={cn(
            "absolute top-full z-50 mt-1.5 max-h-[70vh] min-w-56 overflow-y-auto rounded-lg border bg-raised p-1 shadow-raised",
            align === "end" ? "right-0" : "left-0",
          )}
        >
          {open ? children : null}
        </div>
      </div>
    </MenuContext.Provider>
  );
}

export function MenuItem({
  children,
  onSelect,
  checked,
  description,
  disabled,
}: {
  children: ReactNode;
  onSelect: () => void;
  /** Makes it a `menuitemradio` (e.g. the active organization). */
  checked?: boolean;
  description?: ReactNode;
  disabled?: boolean;
}) {
  const ctx = useContext(MenuContext);
  const select = () => {
    if (disabled) return;
    ctx?.close(false);
    onSelect();
  };
  return (
    <div
      role={checked === undefined ? "menuitem" : "menuitemradio"}
      aria-checked={checked}
      aria-disabled={disabled || undefined}
      tabIndex={-1}
      onClick={select}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          select();
        }
      }}
      className={cn(
        "flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-sm text-foreground-secondary outline-none hover:bg-subtle hover:text-foreground focus:bg-subtle focus:text-foreground",
        disabled && "cursor-not-allowed opacity-60",
      )}
    >
      {checked !== undefined ? (
        <Check aria-hidden="true" className={cn("mt-0.5 size-4 shrink-0", checked ? "text-brand-text" : "invisible")} />
      ) : null}
      <span className="min-w-0 flex-1">
        <span className="block truncate">{children}</span>
        {description ? <span className="block truncate text-xs text-muted-foreground">{description}</span> : null}
      </span>
    </div>
  );
}

export function MenuSeparator() {
  return <div role="separator" className="my-1 h-px bg-border" />;
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <div className="px-2 pt-1.5 pb-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">{children}</div>;
}
