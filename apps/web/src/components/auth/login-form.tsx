"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { GithubButton } from "@/components/auth/github-button";
import { useFormState } from "@/components/auth/use-form-state";
import { Button } from "@/components/ui/button";
import { Callout } from "@/components/ui/callout";
import { Field, FormError, Input } from "@/components/ui/field";
import { ApiError } from "@/lib/api/errors";
import { githubErrorMessage, login, resendVerification, safeNext } from "@/lib/auth";
import { loginSchema } from "@/lib/forms";

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const next = safeNext(params.get("next"));
  const oauthError = githubErrorMessage(params.get("error"));
  const [unverifiedEmail, setUnverifiedEmail] = useState<string | null>(null);
  const [resent, setResent] = useState(false);

  const { bindForm, ...form } = useFormState(
    loginSchema,
    async ({ email, password }) => {
      setUnverifiedEmail(null);
      try {
        await login(email, password);
      } catch (error) {
        if (error instanceof ApiError && error.code === "email_not_verified") setUnverifiedEmail(email);
        throw error;
      }
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      router.replace(next);
    },
    {
      mapError: (error) => {
        if (!(error instanceof ApiError)) return null;
        if (error.code === "invalid_credentials") return "That e-mail and password don't match an account.";
        if (error.code === "email_not_verified") return "Confirm your e-mail address first: open the link we sent you.";
        return null;
      },
    },
  );

  return (
    <AuthCard
      title="Sign in"
      description="Release health for your organization: CI failure risk before deploy, regressions after it."
      footer={
        <>
          <p>
            New to ReleaseLens?{" "}
            <Link href="/signup" className="font-medium text-brand-text hover:underline">
              Create an account
            </Link>
          </p>
          <p>
            <Link href="/forgot-password" className="hover:text-foreground hover:underline">
              Forgot your password?
            </Link>
          </p>
        </>
      }
    >
      {oauthError ? (
        <Callout tone="warning" title="GitHub sign-in did not complete" className="mb-4">
          {oauthError}
        </Callout>
      ) : null}
      <form ref={bindForm} onSubmit={form.onSubmit} noValidate className="space-y-4" aria-label="Sign in">
        <FormError>{form.formError}</FormError>
        {unverifiedEmail ? (
          <div className="text-sm">
            {resent ? (
              <p role="status" className="text-muted-foreground">
                If the address has an account, a new link is on its way.
              </p>
            ) : (
              <Button
                type="button"
                variant="link"
                className="h-auto p-0"
                onClick={async () => {
                  await resendVerification(unverifiedEmail).catch(() => undefined);
                  setResent(true);
                }}
              >
                Send the confirmation link again
              </Button>
            )}
          </div>
        ) : null}
        <Field label="E-mail" error={form.errors.email}>
          {(p) => <Input {...p} name="email" type="email" autoComplete="email" inputMode="email" required />}
        </Field>
        <Field label="Password" error={form.errors.password}>
          {(p) => <Input {...p} name="password" type="password" autoComplete="current-password" required />}
        </Field>
        <Button type="submit" className="w-full" disabled={form.pending}>
          {form.pending ? "Signing in…" : "Sign in"}
        </Button>
      </form>
      <div className="my-5 flex items-center gap-3 text-xs text-muted-foreground" aria-hidden="true">
        <span className="h-px flex-1 bg-border" />
        or
        <span className="h-px flex-1 bg-border" />
      </div>
      <GithubButton />
    </AuthCard>
  );
}
