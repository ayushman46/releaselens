"use client";

import { CircleCheck } from "lucide-react";
import Link from "next/link";
import { useState, useSyncExternalStore } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { useFormState } from "@/components/auth/use-form-state";
import { Button } from "@/components/ui/button";
import { Field, FormError, Input } from "@/components/ui/field";
import { ApiError } from "@/lib/api/errors";
import { confirmPasswordReset, readHashToken, requestPasswordReset } from "@/lib/auth";
import { forgotPasswordSchema, resetPasswordSchema } from "@/lib/forms";

const noopSubscribe = () => () => {};

/** The `#token=` fragment, read on the client only (the server never sees fragments). */
export function useHashToken(): string | null | undefined {
  return useSyncExternalStore(
    noopSubscribe,
    () => readHashToken(),
    () => undefined,
  );
}

export function ForgotPasswordForm() {
  const [sent, setSent] = useState(false);
  const { bindForm, ...form } = useFormState(forgotPasswordSchema, async ({ email }) => {
    await requestPasswordReset(email);
    setSent(true);
  });

  return (
    <AuthCard
      title="Reset your password"
      description="We'll e-mail you a link to choose a new one. It works once and expires in an hour."
      footer={
        <p>
          <Link href="/login" className="font-medium text-brand-text hover:underline">
            Back to sign in
          </Link>
        </p>
      }
    >
      {sent ? (
        <p role="status" className="flex items-start gap-3 text-sm text-foreground-secondary">
          <CircleCheck aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-healthy" />
          If an account uses that address, a reset link is on its way.
        </p>
      ) : (
        <form ref={bindForm} onSubmit={form.onSubmit} noValidate className="space-y-4" aria-label="Reset your password">
          <FormError>{form.formError}</FormError>
          <Field label="E-mail" error={form.errors.email}>
            {(p) => <Input {...p} name="email" type="email" autoComplete="email" inputMode="email" required />}
          </Field>
          <Button type="submit" className="w-full" disabled={form.pending}>
            {form.pending ? "Sending…" : "Send reset link"}
          </Button>
        </form>
      )}
    </AuthCard>
  );
}

export function ResetPasswordForm() {
  const token = useHashToken();
  const [done, setDone] = useState(false);
  const { bindForm, ...form } = useFormState(
    resetPasswordSchema,
    async ({ new_password }) => {
      if (!token) throw new Error("This page needs the link from the reset e-mail.");
      await confirmPasswordReset(token, new_password);
      setDone(true);
    },
    {
      mapError: (error) => {
        if (!(error instanceof ApiError)) return null;
        if (error.code === "invalid_token") return "This reset link is invalid, used or expired. Request a new one.";
        if (error.code === "weak_password") return `Choose a stronger password: ${error.reasons.join("; ") || error.message}.`;
        return null;
      },
    },
  );

  if (done) {
    return (
      <AuthCard title="Password changed" description="Every session was signed out, including other devices.">
        <Button asChild className="w-full">
          <Link href="/login">Sign in with the new password</Link>
        </Button>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title="Choose a new password"
      footer={
        <p>
          <Link href="/forgot-password" className="hover:text-foreground hover:underline">
            Request a new link
          </Link>
        </p>
      }
    >
      {token === null ? (
        <FormError>This page needs the link from the reset e-mail. Open it again, or request a new one.</FormError>
      ) : (
        <form ref={bindForm} onSubmit={form.onSubmit} noValidate className="space-y-4" aria-label="Choose a new password">
          <FormError>{form.formError}</FormError>
          <Field label="New password" hint="At least 12 characters." error={form.errors.new_password}>
            {(p) => <Input {...p} name="new_password" type="password" autoComplete="new-password" required />}
          </Field>
          <Field label="Repeat it" error={form.errors.confirm}>
            {(p) => <Input {...p} name="confirm" type="password" autoComplete="new-password" required />}
          </Field>
          <Button type="submit" className="w-full" disabled={form.pending || token === undefined}>
            {form.pending ? "Saving…" : "Set new password"}
          </Button>
        </form>
      )}
    </AuthCard>
  );
}
