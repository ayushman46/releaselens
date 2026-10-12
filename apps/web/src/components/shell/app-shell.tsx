import type { ReactNode } from "react";

import { AppSidebar } from "@/components/shell/app-sidebar";
import { AppTopbar } from "@/components/shell/app-topbar";

/**
 * Authenticated dashboard frame: sidebar rail from `lg` (1024px) up, a navigation drawer below that
 * (tablet portrait and phones), the top bar, and the main region.
 */
export function AppShell({ banner, children }: { banner?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex min-h-dvh">
      <div className="sticky top-0 hidden h-dvh w-56 shrink-0 border-r bg-panel lg:block">
        <AppSidebar />
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        <AppTopbar />
        {banner}
        <main id="main" tabIndex={-1} className="w-full flex-1 outline-none">
          <div className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6 lg:px-10 lg:py-10">{children}</div>
        </main>
      </div>
    </div>
  );
}
