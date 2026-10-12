"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export function NavLink({ href, children }: { href: string; children: ReactNode }) {
  const pathname = usePathname();
  const active = pathname === href || pathname.startsWith(`${href}/`);

  return (
    <Link
      href={href}
      aria-current={active ? "page" : undefined}
      className={cn(
        "rounded-md px-2 py-1 text-sm transition-colors outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50",
        active
          ? "text-foreground bg-accent/60"
          : "text-muted-foreground hover:text-foreground hover:bg-accent/40",
      )}
    >
      {children}
    </Link>
  );
}
