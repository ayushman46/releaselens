import type { Metadata } from "next";

import { AcceptInvite } from "@/components/auth/token-actions";

export const metadata: Metadata = { title: "Accept invitation" };

export default function AcceptInvitePage() {
  return <AcceptInvite />;
}
