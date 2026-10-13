"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CircleCheck, LoaderCircle } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { AuthCard } from "@/components/auth/auth-card";
import { useHashToken } from "@/components/auth/password-reset";
import { useFormState } from "@/components/auth/use-form-state";
import { Button } from "@/components/ui/button";
import { Field, FormError, Input } from "@/components/ui/field";
import { ApiError, describeApiError } from "@/lib/api/errors";
import { acceptInvite, resendVerification, verifyEmail } from "@/lib/auth";
import { forgotPasswordSchema } from "@/lib/forms";
import { safeStorage } from "@/lib/storage";

/**
 * E-mailed tokens are single use. React's development double-invoke of effects would spend one
 * twice, so each token's request is made once and shared.
 */
const once = new Map<string, Promise<unknown>>();
function runOnce<T>(key: string, fn: () => Promise<T>): Promise<T> {
  let pending = once.get(key) as Promise<T> | undefined;
  if (!pending) {
    pending = fn();
    once.set(key, pending);
  }
  return pending;
}

type Phase = { state: "waiting" } | { state: "done" } | { state: "failed"; error: unknown };

function useTokenAction(token: string | null | undefined, kind: string, action: (token: string) => Promise<unknown>) {
  const [phase, setPhase] = useState<Phase>({ state: "waiting" });
  useEffect(() => {
    if (!token) return;
    let active = true;
    runOnce(`${kind}:${token}`, () => action(token)).then(
      () => active && setPhase({ state: "done" }),
      (error: unknown) => active && setPhase({ state: "failed", error }),
    );
    return () => {
      active = false;
    };
  }, [token, kind, action]);
  return phase;
}

function ResendVerification() {
  const [sent, setSent] = useState(false);
  const { bindForm, ...form } = useFormState(forgotPasswordSchema, async ({ email }) => {
    await resendVerification(email);
    setSent(true);
  });
  if (sent) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        If the address has an unconfirmed account, a new link is on its way.
      </p>
    );
  }
  return (
    <form ref={bindForm} onSubmit={form.onSubmit} noValidate className="space-y-3" aria-label="Send a new confirmation link">
      <FormError>{form.formError}</FormError>
      <Field label="E-mail" error={form.errors.email}>
        {(p) => <Input {...p} name="email" type="email" autoComplete="email" />}
      </Field>
      <Button type="submit" variant="outline" size="sm" disabled={form.pending}>
        Send a new link
      </Button>
    </form>
  );
}

export function VerifyEmail() {
  const token = useHashToken();
  const phase = useTokenAction(token, "verify", verifyEmail);

  if (token === null) {
    return (
      <AuthCard title="Confirm your e-mail" description="Open the confirmation link from the e-mail we sent you. Lost it? Request a new one.">
        <ResendVerification />
      </AuthCard>
    );
  }
  if (phase.state === "done") {
    return (
      <AuthCard title="E-mail confirmed" description="Your address is confirmed. Sign in to set up your organization.">
        <p role="status" className="sr-only">
          E-mail confirmed
        </p>
        <Button asChild className="w-full">
          <Link href="/login">
            <CircleCheck aria-hidden="true" />
            Sign in
          </Link>
        </Button>
      </AuthCard>
    );
  }
  if (phase.state === "failed") {
    const message =
      phase.error instanceof ApiError && phase.error.code === "invalid_token"
        ? "This link is invalid, already used or expired."
        : describeApiError(phase.error);
    return (
      <AuthCard title="Could not confirm your e-mail" description={message}>
        <ResendVerification />
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Confirming your e-mail…">
      <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
        <LoaderCircle aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" />
        Checking the link
      </p>
    </AuthCard>
  );
}

const PENDING_INVITE_KEY = "rl.pendingInvite";

/** Remembered across the sign-in redirect (the fragment does not survive `?next=`). */
export function pendingInviteToken(): string | null {
  return safeStorage.get(PENDING_INVITE_KEY);
}

export function AcceptInvite() {
  const hashToken = useHashToken();
  const queryClient = useQueryClient();
  const [stored] = useState(() => pendingInviteToken());
  const token = hashToken ?? (hashToken === null ? stored : undefined);
  const phase = useTokenAction(token, "invite", async (t) => {
    const accepted = await acceptInvite(t);
    safeStorage.remove(PENDING_INVITE_KEY);
    safeStorage.set("rl.activeOrg", accepted.org_id);
    await queryClient.invalidateQueries({ queryKey: ["me"] });
    return accepted;
  });

  if (!token) {
    return (
      <AuthCard title="Join an organization" description="Open the invitation link from your e-mail. It works once and expires after a few days." />
    );
  }
  if (phase.state === "done") {
    return (
      <AuthCard title="You're in" description="The invitation is accepted. The organization is now in your switcher.">
        <Button asChild className="w-full">
          <Link href="/app">Open the dashboard</Link>
        </Button>
      </AuthCard>
    );
  }
  if (phase.state === "failed") {
    const error = phase.error;
    if (error instanceof ApiError && error.status === 401) {
      safeStorage.set(PENDING_INVITE_KEY, token);
      return (
        <AuthCard title="Sign in to accept" description="Sign in (or create an account with the invited address), then come back to this page.">
          <div className="flex flex-col gap-2">
            <Button asChild className="w-full">
              <Link href="/login?next=/invites/accept">Sign in</Link>
            </Button>
            <Button asChild variant="outline" className="w-full">
              <Link href="/signup">Create an account</Link>
            </Button>
          </div>
        </AuthCard>
      );
    }
    const message =
      error instanceof ApiError && (error.code === "invalid_token" || error.status === 400)
        ? "This invitation is invalid, already used or expired. Ask an admin to send a new one."
        : describeApiError(error);
    return <AuthCard title="Could not accept the invitation" description={message} />;
  }
  return (
    <AuthCard title="Accepting the invitation…">
      <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
        <LoaderCircle aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" />
        Checking the link
      </p>
    </AuthCard>
  );
}
