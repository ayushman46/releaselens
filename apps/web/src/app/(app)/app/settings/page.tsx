import type { Metadata } from "next";

import { SettingsTabs } from "@/components/shell/settings-tabs";
import { PageHeader } from "@/components/ui/page-header";

export const metadata: Metadata = { title: "Settings" };

export default function SettingsPage() {
  return (
    <>
      <PageHeader
        title="Settings"
        description="Your organization, API keys, integrations and plan."
        breadcrumbs={[{ label: "Dashboard", href: "/app" }, { label: "Settings" }]}
      />
      <SettingsTabs />
    </>
  );
}
