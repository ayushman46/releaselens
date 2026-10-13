"use client";

import { useRef, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";

/** Asks before an irreversible action. Focus starts on Cancel, the safe choice. */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  pending,
  onConfirm,
  onClose,
  destructive = true,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  pending?: boolean;
  onConfirm: () => void;
  onClose: () => void;
  destructive?: boolean;
}) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  return (
    <Dialog open={open} onClose={onClose} label={title} initialFocus={cancelRef} className="p-6">
      <h2 className="text-base font-semibold">{title}</h2>
      <div className="mt-2 text-sm leading-6 text-muted-foreground">{children}</div>
      <div className="mt-6 flex justify-end gap-2">
        <Button ref={cancelRef} variant="ghost" size="sm" onClick={onClose} disabled={pending}>
          Cancel
        </Button>
        <Button variant={destructive ? "destructive" : "default"} size="sm" onClick={onConfirm} disabled={pending}>
          {pending ? "Working…" : confirmLabel}
        </Button>
      </div>
    </Dialog>
  );
}
