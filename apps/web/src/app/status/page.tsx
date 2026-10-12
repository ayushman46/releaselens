import type { Metadata } from "next";

import { StatusDashboard } from "@/components/status/status-dashboard";

export const metadata: Metadata = {
  title: "System status",
};

export default function StatusPage() {
  // The dashboard is a client component: probes run in the browser against
  // NEXT_PUBLIC_RL_API_URL, so this page reflects what users can actually reach.
  return <StatusDashboard />;
}
