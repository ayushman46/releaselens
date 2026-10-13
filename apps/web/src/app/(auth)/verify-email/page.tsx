import type { Metadata } from "next";

import { VerifyEmail } from "@/components/auth/token-actions";

export const metadata: Metadata = { title: "Confirm your e-mail" };

export default function VerifyEmailPage() {
  return <VerifyEmail />;
}
