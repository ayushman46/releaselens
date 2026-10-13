import type { Metadata } from "next";
import { Suspense } from "react";

import { BillingSettings } from "@/components/settings/billing-settings";
import { LoadingState } from "@/components/ui/skeleton";

export const metadata: Metadata = { title: "Billing · Settings" };

export default function BillingPage() {
  return (
    <Suspense fallback={<LoadingState label="Loading billing…" lines={4} />}>
      <BillingSettings />
    </Suspense>
  );
}
