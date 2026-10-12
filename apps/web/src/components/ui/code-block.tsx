"use client";

import { Check, Copy, TextSelect } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

type CopyState = "idle" | "copied" | "selected";

/** Selects the element's text so the user can copy it manually; returns whether it worked. */
function selectContents(element: HTMLElement | null): boolean {
  const selection = typeof window === "undefined" ? null : window.getSelection();
  if (!element || !selection) return false;
  const range = document.createRange();
  range.selectNodeContents(element);
  selection.removeAllRanges();
  selection.addRange(range);
  return true;
}

/**
 * Read-only code with a copy button. The <pre> is focusable so keyboard users can scroll long
 * lines; the copy result is announced through a polite live region. When the Clipboard API is
 * unavailable (permissions, embedded frames) it falls back to selecting the code and
 * `execCommand("copy")`, and if that fails too, leaves the code selected for a manual copy.
 */
export function CodeBlock({
  code,
  title,
  language,
  className,
}: {
  code: string;
  /** File name or short caption shown in the header. */
  title?: string;
  /** Language label shown in the header ("bash", "yaml"…). */
  language?: string;
  className?: string;
}) {
  const [state, setState] = useState<CopyState>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const preRef = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const t = timer;
    return () => {
      if (t.current) clearTimeout(t.current);
    };
  }, []);

  const copy = async () => {
    let next: CopyState;
    try {
      await navigator.clipboard.writeText(code);
      next = "copied";
    } catch {
      const selected = selectContents(preRef.current);
      let copied = false;
      try {
        copied = selected && document.execCommand("copy");
      } catch {
        copied = false;
      }
      next = copied ? "copied" : "selected";
    }
    setState(next);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setState("idle"), next === "copied" ? 2000 : 4000);
  };

  const caption = title ?? language ?? "Code";

  return (
    <figure
      data-slot="code-block"
      className={cn("overflow-hidden rounded-lg border bg-panel text-left", className)}
    >
      <figcaption className="flex h-9 items-center justify-between gap-2 border-b bg-subtle/60 pr-1.5 pl-3">
        <span className="truncate font-mono text-xs text-muted-foreground">{caption}</span>
        <button
          type="button"
          onClick={copy}
          aria-label={`Copy ${caption}`}
          className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-subtle hover:text-foreground"
        >
          {state === "copied" ? (
            <Check aria-hidden="true" className="size-3.5 text-healthy" />
          ) : state === "selected" ? (
            <TextSelect aria-hidden="true" className="size-3.5" />
          ) : (
            <Copy aria-hidden="true" className="size-3.5" />
          )}
          <span aria-hidden="true">
            {state === "copied" ? "Copied" : state === "selected" ? "Selected, copy manually" : "Copy"}
          </span>
        </button>
      </figcaption>
      <pre
        ref={preRef}
        tabIndex={0}
        className="overflow-x-auto p-4 font-mono text-[12.5px] leading-6 text-foreground"
      >
        <code>{code}</code>
      </pre>
      <span aria-live="polite" className="sr-only">
        {state === "copied"
          ? "Copied to clipboard"
          : state === "selected"
            ? "Could not copy automatically. The code is selected; copy it with your keyboard."
            : ""}
      </span>
    </figure>
  );
}
