import type { Metadata } from "next";
import { Suspense } from "react";

import { AuditLogSettings } from "@/components/settings/audit-log-settings";
import { LoadingState } from "@/components/ui/skeleton";

export const metadata: Metadata = { title: "Audit log · Settings" };

export default function AuditLogPage() {
  return (
    <Suspense fallback={<LoadingState label="Loading the audit log…" lines={6} />}>
      <AuditLogSettings />
    </Suspense>
  );
}
