"use client";

import { Activity, BookOpen } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useId } from "react";

import { LogoMark } from "@/components/logo-mark";
import { Badge } from "@/components/ui/badge";
import { APP_HOME, APP_NAV, isActivePath, type NavItem } from "@/lib/navigation";
import { cn } from "@/lib/utils";

function SidebarLink({
  item,
  active,
  onNavigate,
}: {
  item: NavItem;
  active: boolean;
  onNavigate?: () => void;
}) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      onClick={onNavigate}
      className={cn(
        "group relative flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] font-medium transition-colors",
        active
          ? "bg-subtle text-foreground"
          : "text-foreground-secondary hover:bg-subtle/70 hover:text-foreground",
      )}
    >
      {active ? (
        <span aria-hidden="true" className="absolute top-1.5 bottom-1.5 -left-2 w-0.5 rounded-full bg-brand" />
      ) : null}
      <Icon
        aria-hidden="true"
        className={cn(
          "size-4 shrink-0",
          active ? "text-brand-text" : "text-muted-foreground group-hover:text-foreground-secondary",
        )}
      />
      <span className="truncate">{item.label}</span>
    </Link>
  );
}

/** Dashboard navigation. Rendered in the desktop rail and inside the mobile drawer. */
export function AppSidebar({ onNavigate, className }: { onNavigate?: () => void; className?: string }) {
  const pathname = usePathname() ?? "";
  const idPrefix = useId();

  return (
    <div className={cn("flex h-full flex-col", className)}>
      <div className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
        <Link
          href={APP_HOME}
          onClick={onNavigate}
          className="flex items-center gap-2 rounded-md text-sm font-semibold tracking-tight"
        >
          <LogoMark className="size-5 text-foreground" />
          ReleaseLens
        </Link>
        {/* In the mobile drawer the dialog's close button sits here instead. */}
        {onNavigate ? null : (
          <Badge variant="subtle" className="ml-auto">
            Preview
          </Badge>
        )}
      </div>

      <nav aria-label="Dashboard" className="flex-1 overflow-y-auto px-3 py-4">
        {APP_NAV.map((group) => {
          const labelId = `${idPrefix}-${group.label.toLowerCase()}`;
          return (
            <div key={group.label} className="mb-5 last:mb-0">
              <p id={labelId} className="px-2 pb-1.5 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
                {group.label}
              </p>
              <ul aria-labelledby={labelId} className="space-y-0.5">
                {group.items.map((item) => (
                  <li key={item.href}>
                    <SidebarLink item={item} active={isActivePath(pathname, item.href)} onNavigate={onNavigate} />
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </nav>

      <div className="shrink-0 space-y-0.5 border-t px-3 py-3">
        <Link
          href="/docs"
          onClick={onNavigate}
          className="flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] text-foreground-secondary hover:bg-subtle/70 hover:text-foreground"
        >
          <BookOpen aria-hidden="true" className="size-4 text-muted-foreground" />
          Documentation
        </Link>
        <Link
          href="/status"
          onClick={onNavigate}
          className="flex h-8 items-center gap-2.5 rounded-md px-2 text-[13px] text-foreground-secondary hover:bg-subtle/70 hover:text-foreground"
        >
          <Activity aria-hidden="true" className="size-4 text-muted-foreground" />
          System status
        </Link>
      </div>
    </div>
  );
}
