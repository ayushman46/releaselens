"use client";

import {
  cloneElement,
  isValidElement,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";

import { cn } from "@/lib/utils";

type Side = "top" | "bottom" | "left" | "right";
type Align = "start" | "center" | "end";

const SIDE_CLASSES: Record<Side, string> = {
  top: "bottom-full mb-2",
  bottom: "top-full mt-2",
  left: "right-full mr-2 top-1/2 -translate-y-1/2",
  right: "left-full ml-2 top-1/2 -translate-y-1/2",
};

const ALIGN_CLASSES: Record<Align, string> = {
  start: "left-0",
  center: "left-1/2 -translate-x-1/2",
  end: "right-0",
};

const OPEN_DELAY_MS = 350;

function isFocusVisible(target: EventTarget): boolean {
  try {
    return target instanceof Element && target.matches(":focus-visible");
  } catch {
    // Engines without :focus-visible support: treat every focus as keyboard focus.
    return true;
  }
}

/**
 * Accessible tooltip (WAI-ARIA tooltip pattern + WCAG 1.4.13):
 * - the trigger gets `aria-describedby`, so the text is announced even while hidden;
 * - opens on hover (after a short delay) and immediately on keyboard focus;
 * - stays open while the pointer is over the tooltip itself (hoverable);
 * - Escape dismisses it without moving focus.
 *
 * The child must be a single focusable element (a button or link) that accepts aria props.
 */
export function Tooltip({
  content,
  children,
  side = "top",
  align = "center",
  className,
}: {
  content: ReactNode;
  children: ReactElement<{ "aria-describedby"?: string }>;
  side?: Side;
  align?: Align;
  className?: string;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clear = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };
  const show = (delay: number) => {
    clear();
    if (delay === 0) setOpen(true);
    else timer.current = setTimeout(() => setOpen(true), delay);
  };
  const hide = () => {
    clear();
    setOpen(false);
  };

  useEffect(() => clear, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  const describedBy = isValidElement(children)
    ? [children.props["aria-describedby"], id].filter(Boolean).join(" ")
    : id;

  return (
    <span
      className="relative inline-flex"
      onPointerEnter={(event) => {
        if (event.pointerType === "mouse") show(OPEN_DELAY_MS);
      }}
      onPointerLeave={hide}
      onFocus={(event) => {
        // Only keyboard focus (focus-visible) opens instantly; a mouse click should not.
        if (isFocusVisible(event.target)) show(0);
      }}
      onBlur={hide}
    >
      {cloneElement(children, { "aria-describedby": describedBy })}
      <span
        id={id}
        role="tooltip"
        hidden={!open}
        className={cn(
          "absolute z-50 w-max max-w-64 rounded-md border bg-raised px-2.5 py-1.5 text-xs leading-5 font-normal whitespace-normal text-foreground shadow-raised animate-fade-in",
          SIDE_CLASSES[side],
          side === "top" || side === "bottom" ? ALIGN_CLASSES[align] : null,
          className,
        )}
      >
        {content}
      </span>
    </span>
  );
}
