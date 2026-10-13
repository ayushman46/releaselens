"use client";

import { MailCheck } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { GithubButton } from "@/components/auth/github-button";
import { useFormState } from "@/components/auth/use-form-state";
import { Button } from "@/components/ui/button";
import { Field, FormError, Input } from "@/components/ui/field";
import { ApiError } from "@/lib/api/errors";
import { resendVerification, signup } from "@/lib/auth";
import { signupSchema } from "@/lib/forms";

export function SignupForm() {
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [resent, setResent] = useState(false);

  const { bindForm, ...form } = useFormState(
    signupSchema,
    async ({ email, password, display_name }) => {
      await signup(email, password, display_name);
      setSentTo(email);
    },
    {
      mapError: (error) =>
        error instanceof ApiError && error.code === "weak_password"
          ? `Choose a stronger password: ${error.reasons.join("; ") || error.message}.`
          : null,
    },
  );

  if (sentTo) {
    return (
      <AuthCard
        title="Check your inbox"
        description={
          <>
            If <span className="font-medium text-foreground">{sentTo}</span> can be used for a new account, a
            confirmation link is on its way. It works once and expires in 24 hours.
          </>
        }
        footer={
          <p>
            Confirmed already?{" "}
            <Link href="/login" className="font-medium text-brand-text hover:underline">
              Sign in
            </Link>
          </p>
        }
      >
        <div role="status" className="flex items-start gap-3 text-sm text-foreground-secondary">
          <MailCheck aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-healthy" />
          <p>Open the link from the e-mail on this device. Nothing arrived after a few minutes? Check spam, or send it again.</p>
        </div>
        <div className="mt-4">
          {resent ? (
            <p role="status" className="text-sm text-muted-foreground">
              Sent again.
            </p>
          ) : (
            <Button
              variant="outline"
              size="sm"
              onClick={async () => {
                await resendVerification(sentTo).catch(() => undefined);
                setResent(true);
              }}
            >
              Send the link again
            </Button>
          )}
        </div>
      </AuthCard>
    );
  }

  return (
    <AuthCard
      title="Create your account"
      description="Free for one project. No credit card."
      footer={
        <p>
          Already have an account?{" "}
          <Link href="/login" className="font-medium text-brand-text hover:underline">
            Sign in
          </Link>
        </p>
      }
    >
      <form ref={bindForm} onSubmit={form.onSubmit} noValidate className="space-y-4" aria-label="Create your account">
        <FormError>{form.formError}</FormError>
        <Field label="Name" optional error={form.errors.display_name}>
          {(p) => <Input {...p} name="display_name" autoComplete="name" />}
        </Field>
        <Field label="Work e-mail" error={form.errors.email}>
          {(p) => <Input {...p} name="email" type="email" autoComplete="email" inputMode="email" required />}
        </Field>
        <Field
          label="Password"
          hint="At least 12 characters. Common words, keyboard runs and your e-mail name are refused."
          error={form.errors.password}
        >
          {(p) => <Input {...p} name="password" type="password" autoComplete="new-password" required />}
        </Field>
        <Button type="submit" className="w-full" disabled={form.pending}>
          {form.pending ? "Creating account…" : "Create account"}
        </Button>
      </form>
      <div className="my-5 flex items-center gap-3 text-xs text-muted-foreground" aria-hidden="true">
        <span className="h-px flex-1 bg-border" />
        or
        <span className="h-px flex-1 bg-border" />
      </div>
      <GithubButton label="Sign up with GitHub" />
    </AuthCard>
  );
}
