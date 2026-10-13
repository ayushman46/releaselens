import type { Metadata } from "next";

import { RiskDetailView } from "@/components/risk/risk-views";

export const metadata: Metadata = { title: "Risk assessment" };

export default async function RiskAssessmentPage({ params }: { params: Promise<{ assessmentId: string }> }) {
  const { assessmentId } = await params;
  return <RiskDetailView assessmentId={assessmentId} />;
}
