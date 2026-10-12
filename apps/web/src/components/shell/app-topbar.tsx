"use client";

import { Building, ChevronDown, ChevronsUpDown, Layers, Menu } from "lucide-react";
import { useState } from "react";

import { ThemeToggle } from "@/components/theme-toggle";
import { AppSidebar } from "@/components/shell/app-sidebar";
import { CommandMenuTrigger } from "@/components/shell/command-menu";
import { Dialog } from "@/components/ui/dialog";

/** Placeholder until organizations and sign-in exist (M10). Disabled, and says why. */
function OrgSwitcherPlaceholder() {
  return (
    <button
      type="button"
      disabled
      aria-label="Organization switcher, unavailable: sign-in arrives in M10"
      className="inline-flex h-9 min-w-0 shrink cursor-not-allowed items-center gap-2 rounded-md px-1.5 text-left"
    >
      <span className="flex size-6 shrink-0 items-center justify-center rounded-md border bg-subtle">
        <Building aria-hidden="true" className="size-3.5 text-muted-foreground" />
      </span>
      <span className="flex min-w-0 flex-col leading-tight">
        <span className="truncate text-[13px] font-medium text-foreground-secondary">Organization</span>
        <span className="text-[11px] whitespace-nowrap text-muted-foreground">Sign-in arrives in M10</span>
      </span>
      <ChevronsUpDown aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
    </button>
  );
}

/** Placeholder until deployment events (M6) tell us which environments exist. */
function EnvironmentPlaceholder() {
  return (
    <button
      type="button"
      disabled
      aria-label="Environment selector, unavailable: environments appear with your first deployment event"
      className="hidden h-8 shrink-0 cursor-not-allowed items-center gap-1.5 rounded-md border border-dashed px-2.5 text-[13px] whitespace-nowrap text-muted-foreground sm:inline-flex"
    >
      <Layers aria-hidden="true" className="size-3.5" />
      All environments
      <ChevronDown aria-hidden="true" className="size-3.5" />
    </button>
  );
}

export function AppTopbar() {
  const [navOpen, setNavOpen] = useState(false);

  return (
    <header className="sticky top-0 z-30 flex h-12 shrink-0 items-center gap-2 border-b bg-background px-3 sm:px-4">
      <button
        type="button"
        onClick={() => setNavOpen(true)}
        aria-label="Open navigation"
        aria-haspopup="dialog"
        aria-expanded={navOpen}
        className="inline-flex size-8 shrink-0 items-center justify-center rounded-md text-foreground-secondary hover:bg-subtle hover:text-foreground lg:hidden"
      >
        <Menu aria-hidden="true" className="size-4" />
      </button>

      <OrgSwitcherPlaceholder />
      <span aria-hidden="true" className="hidden text-border-strong sm:inline">
        /
      </span>
      <EnvironmentPlaceholder />

      <div className="ml-auto flex shrink-0 items-center gap-1.5">
        <CommandMenuTrigger />
        <ThemeToggle />
      </div>

      <Dialog open={navOpen} onClose={() => setNavOpen(false)} label="Navigation" placement="left" showClose>
        <AppSidebar onNavigate={() => setNavOpen(false)} />
      </Dialog>
    </header>
  );
}
