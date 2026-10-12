"use client";

import { X } from "lucide-react";
import {
  useEffect,
  useId,
  useRef,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { createPortal } from "react-dom";

import { cn } from "@/lib/utils";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => !el.hasAttribute("hidden") && el.getAttribute("aria-hidden") !== "true",
  );
}

/**
 * Modal dialog without a dependency:
 * - rendered in a portal on <body> with `role="dialog"` + `aria-modal`;
 * - moves focus inside on open (to `initialFocus` or the first focusable element),
 *   traps Tab / Shift+Tab, closes on Escape or a backdrop click;
 * - returns focus to whatever was focused before it opened;
 * - locks page scroll while open.
 */
export function Dialog({
  open,
  onClose,
  label,
  description,
  initialFocus,
  placement = "center",
  showClose = true,
  className,
  children,
}: {
  open: boolean;
  onClose: () => void;
  /** Accessible name of the dialog. */
  label: string;
  /** Optional accessible description (rendered visually hidden). */
  description?: string;
  initialFocus?: RefObject<HTMLElement | null>;
  placement?: "center" | "top" | "left";
  showClose?: boolean;
  className?: string;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);
  const descriptionId = useId();
  const onCloseRef = useRef(onClose);
  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    const target = initialFocus?.current ?? (panel ? focusables(panel)[0] : undefined) ?? panel;
    target?.focus();

    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = overflow;
      // Restore focus after the portal unmounts.
      if (previouslyFocused && document.contains(previouslyFocused)) previouslyFocused.focus();
    };
  }, [open, initialFocus]);

  if (!open || typeof document === "undefined") return null;

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onCloseRef.current();
      return;
    }
    if (event.key !== "Tab" || !panelRef.current) return;
    const items = focusables(panelRef.current);
    if (items.length === 0) {
      event.preventDefault();
      return;
    }
    const first = items[0]!;
    const last = items[items.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return createPortal(
    <div
      className={cn(
        "fixed inset-0 z-50 flex",
        placement === "center" && "items-center justify-center p-4",
        placement === "top" && "items-start justify-center px-4 pt-[12vh]",
        placement === "left" && "items-stretch justify-start",
      )}
      onKeyDown={onKeyDown}
    >
      <div
        aria-hidden="true"
        className="absolute inset-0 bg-overlay animate-fade-in"
        onMouseDown={() => onCloseRef.current()}
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={label}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={cn(
          "relative border bg-raised text-foreground shadow-raised",
          placement === "left"
            ? "h-full w-72 max-w-[85vw] border-y-0 border-l-0 animate-slide-in-left"
            : "w-full max-w-lg rounded-xl animate-pop-in",
          className,
        )}
      >
        {description ? (
          <p id={descriptionId} className="sr-only">
            {description}
          </p>
        ) : null}
        {children}
        {showClose ? (
          <button
            type="button"
            onClick={() => onCloseRef.current()}
            aria-label="Close"
            className="absolute top-3 right-3 inline-flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-subtle hover:text-foreground"
          >
            <X aria-hidden="true" className="size-4" />
          </button>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}
