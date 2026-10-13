import type { ReactNode } from "react";

import { SettingsNav } from "@/components/settings/settings-nav";
import { PageHeader } from "@/components/ui/page-header";

export default function SettingsLayout({ children }: { children: ReactNode }) {
  return (
    <>
      <PageHeader
        title="Settings"
        description="Your organization, members, API keys, alerts, audit log and plan."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Settings" }]}
      />
      <SettingsNav />
      {children}
    </>
  );
}
