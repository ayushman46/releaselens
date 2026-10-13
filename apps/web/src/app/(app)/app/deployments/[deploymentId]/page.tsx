import type { Metadata } from "next";

import { DeploymentDetailView } from "@/components/deployments/deployment-detail";

export const metadata: Metadata = { title: "Deployment" };

export default async function DeploymentPage({ params }: { params: Promise<{ deploymentId: string }> }) {
  const { deploymentId } = await params;
  return <DeploymentDetailView deploymentId={deploymentId} />;
}
