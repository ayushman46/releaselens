"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { DOCS } from "@/lib/docs";
import { isActivePath } from "@/lib/navigation";
import { cn } from "@/lib/utils";

const ITEMS = [{ href: "/docs", label: "Overview" }, ...DOCS.map((d) => ({ href: `/docs/${d.slug}`, label: d.title }))];

export function DocsNav() {
  const pathname = usePathname() ?? "";
  return (
    <nav aria-label="Documentation">
      <p className="px-2.5 pb-2 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">Docs</p>
      <ul className="relative flex gap-1 overflow-x-auto pb-1 md:flex-col md:gap-0.5 md:overflow-visible md:pb-0">
        {ITEMS.map((item) => {
          const active = item.href === "/docs" ? pathname === "/docs" : isActivePath(pathname, item.href);
          return (
            <li key={item.href} className="shrink-0">
              <Link
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex h-8 items-center rounded-md px-2.5 text-sm whitespace-nowrap transition-colors",
                  active
                    ? "bg-subtle font-medium text-foreground"
                    : "text-muted-foreground hover:bg-subtle/60 hover:text-foreground",
                )}
              >
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
