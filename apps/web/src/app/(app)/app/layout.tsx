import type { Metadata } from "next";
import type { ReactNode } from "react";

import { SessionGate } from "@/components/session/session-gate";
import { SessionProvider } from "@/components/session/session-provider";
import { AppShell } from "@/components/shell/app-shell";

export const metadata: Metadata = {
  // The dashboard is per-organization; keep it out of search results.
  robots: { index: false, follow: false },
};

export default function DashboardLayout({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <AppShell>
        <SessionGate>{children}</SessionGate>
      </AppShell>
    </SessionProvider>
  );
}
