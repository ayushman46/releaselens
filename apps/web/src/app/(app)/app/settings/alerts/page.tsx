import type { Metadata } from "next";

import { AlertsSettings } from "@/components/settings/alerts-settings";

export const metadata: Metadata = { title: "Alerts · Settings" };

export default function AlertsPage() {
  return <AlertsSettings />;
}
