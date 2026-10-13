import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LoginForm } from "@/components/auth/login-form";
import { SignupForm } from "@/components/auth/signup-form";

const nav = vi.hoisted(() => ({ replace: vi.fn(), params: new URLSearchParams() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: nav.replace, push: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => nav.params,
  usePathname: () => "/login",
}));

function wrap(node: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

const fetchMock = vi.fn();

beforeEach(() => {
  nav.replace.mockReset();
  nav.params = new URLSearchParams();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function submit(form: HTMLElement) {
  await act(async () => {
    fireEvent.submit(form);
  });
}

describe("LoginForm", () => {
  it("validates with zod before calling the API and focuses the first invalid field", async () => {
    wrap(<LoginForm />);
    await submit(screen.getByRole("form", { name: "Sign in" }));
    const email = screen.getByLabelText("E-mail");
    expect(email).toHaveAttribute("aria-invalid", "true");
    expect(email).toHaveAccessibleDescription("Enter your e-mail address.");
    expect(screen.getByLabelText("Password")).toHaveAccessibleDescription("Enter your password.");
    expect(document.activeElement).toBe(email);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the API's wrong-credentials answer as a form error", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "invalid_credentials", message: "invalid e-mail or password", request_id: "r" } }), { status: 401, headers: { "content-type": "application/json" } }),
    );
    wrap(<LoginForm />);
    fireEvent.change(screen.getByLabelText("E-mail"), { target: { value: "dev@acme.io" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "whatever-password" } });
    await submit(screen.getByRole("form", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That e-mail and password don't match an account.");
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("signs in and goes to the safe `next` path", async () => {
    nav.params = new URLSearchParams("next=/app/deployments");
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ user: { id: "u" }, csrf_token: "c", access_expires_at: "x", refresh_expires_at: "y" }), { status: 200, headers: { "content-type": "application/json" } }),
    );
    wrap(<LoginForm />);
    fireEvent.change(screen.getByLabelText("E-mail"), { target: { value: "dev@acme.io" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "whatever-password" } });
    await submit(screen.getByRole("form", { name: "Sign in" }));
    expect(nav.replace).toHaveBeenCalledWith("/app/deployments");
  });

  it("explains a GitHub callback error from ?error=", () => {
    nav.params = new URLSearchParams("error=github_no_verified_email");
    wrap(<LoginForm />);
    expect(screen.getByText(/no verified primary e-mail address/)).toBeInTheDocument();
  });
});

describe("SignupForm", () => {
  it("rejects a short password client-side with an accessible error", async () => {
    wrap(<SignupForm />);
    fireEvent.change(screen.getByLabelText("Work e-mail"), { target: { value: "dev@acme.io" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "short" } });
    await submit(screen.getByRole("form", { name: "Create your account" }));
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Use at least 12 characters.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the API's weak_password reasons", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "weak_password", message: "weak", details: { reasons: ["is a common password"] } } }), { status: 422, headers: { "content-type": "application/json" } }),
    );
    wrap(<SignupForm />);
    fireEvent.change(screen.getByLabelText("Work e-mail"), { target: { value: "dev@acme.io" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "Password123!x" } });
    await submit(screen.getByRole("form", { name: "Create your account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a stronger password: is a common password.");
  });

  it("asks the user to check their inbox after a 202", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ status: "accepted", message: "check your inbox" }), { status: 202, headers: { "content-type": "application/json" } }));
    wrap(<SignupForm />);
    fireEvent.change(screen.getByLabelText("Work e-mail"), { target: { value: "dev@acme.io" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "correct horse battery staple" } });
    await submit(screen.getByRole("form", { name: "Create your account" }));
    expect(await screen.findByRole("heading", { name: "Check your inbox" })).toBeInTheDocument();
  });
});
