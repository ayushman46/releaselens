import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppSidebar } from "@/components/shell/app-sidebar";
import { APP_NAV, isActivePath } from "@/lib/navigation";

const nav = vi.hoisted(() => ({ pathname: "/app" }));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ push: vi.fn(), prefetch: vi.fn() }),
}));

const LABELS = APP_NAV.flatMap((group) => group.items.map((item) => item.label));

function currentLinks(): string[] {
  const navigation = screen.getByRole("navigation", { name: "Dashboard" });
  return within(navigation)
    .getAllByRole("link")
    .filter((link) => link.getAttribute("aria-current") === "page")
    .map((link) => link.textContent ?? "");
}

describe("AppSidebar", () => {
  it("lists every dashboard section", () => {
    render(<AppSidebar />);
    const navigation = screen.getByRole("navigation", { name: "Dashboard" });
    expect(within(navigation).getAllByRole("link").map((l) => l.textContent)).toEqual(LABELS);
    expect(LABELS).toEqual([
      "Overview",
      "Deployments",
      "Regressions",
      "Risk analysis",
      "Services",
      "Service map",
      "Analytics",
      "Model",
      "Settings",
    ]);
  });

  it.each([
    ["/app", "Overview"],
    ["/app/deployments", "Deployments"],
    ["/app/deployments/0193-abc", "Deployments"],
    ["/app/service-map", "Service map"],
    ["/app/settings", "Settings"],
  ])("on %s marks only %s with aria-current=page", (pathname, label) => {
    nav.pathname = pathname;
    render(<AppSidebar />);
    expect(currentLinks()).toEqual([label]);
  });

  it("does not confuse Services with Service map", () => {
    nav.pathname = "/app/services";
    render(<AppSidebar />);
    expect(currentLinks()).toEqual(["Services"]);
  });
});

describe("isActivePath", () => {
  it("matches the dashboard root and the home page exactly", () => {
    expect(isActivePath("/app", "/app")).toBe(true);
    expect(isActivePath("/app/regressions", "/app")).toBe(false);
    expect(isActivePath("/pricing", "/")).toBe(false);
  });

  it("matches sub-pages but not prefixes of other words", () => {
    expect(isActivePath("/docs/methodology", "/docs")).toBe(true);
    expect(isActivePath("/app/service-map", "/app/services")).toBe(false);
  });
});
