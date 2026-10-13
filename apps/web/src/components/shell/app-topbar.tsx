"use client";

import { Menu } from "lucide-react";
import { useState } from "react";

import { OrgSwitcher, UserMenu } from "@/components/session/org-switcher";
import { ThemeToggle } from "@/components/theme-toggle";
import { AppSidebar } from "@/components/shell/app-sidebar";
import { CommandMenuTrigger } from "@/components/shell/command-menu";
import { Dialog } from "@/components/ui/dialog";

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

      <OrgSwitcher />

      <div className="ml-auto flex shrink-0 items-center gap-1.5">
        <CommandMenuTrigger />
        <ThemeToggle />
        <UserMenu />
      </div>

      <Dialog open={navOpen} onClose={() => setNavOpen(false)} label="Navigation" placement="left" showClose>
        <AppSidebar onNavigate={() => setNavOpen(false)} />
      </Dialog>
    </header>
  );
}
