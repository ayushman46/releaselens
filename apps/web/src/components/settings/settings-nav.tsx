"use client";

import { Bell, Building, CreditCard, KeyRound, ScrollText, Users } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

export const SETTINGS_SECTIONS = [
  { href: "/app/settings", label: "General", icon: Building },
  { href: "/app/settings/members", label: "Members", icon: Users },
  { href: "/app/settings/api-keys", label: "API keys", icon: KeyRound },
  { href: "/app/settings/alerts", label: "Alerts", icon: Bell },
  { href: "/app/settings/audit-log", label: "Audit log", icon: ScrollText },
  { href: "/app/settings/billing", label: "Billing", icon: CreditCard },
];

export function SettingsNav() {
  const pathname = usePathname() ?? "";
  return (
    <nav aria-label="Settings sections" className="-mx-1 mb-6 overflow-x-auto border-b [scrollbar-width:none]">
      <ul className="flex min-w-max gap-1 px-1">
        {SETTINGS_SECTIONS.map((s) => {
          const active = pathname === s.href;
          const Icon = s.icon;
          return (
            <li key={s.href}>
              <Link
                href={s.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "relative -mb-px inline-flex h-9 items-center gap-1.5 rounded-t-md border-b-2 px-3 text-sm font-medium whitespace-nowrap",
                  active ? "border-brand text-foreground" : "border-transparent text-muted-foreground hover:text-foreground",
                )}
              >
                <Icon aria-hidden="true" className="size-4" />
                {s.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
