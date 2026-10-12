import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { getStatusMeta, STATUS_META, StatusBadge } from "@/components/status/status-badge";
import type { HealthState } from "@/lib/status";

const EXPECTED: Array<[HealthState, string, string]> = [
  ["ok", "OK", "success"],
  ["ready", "Ready", "success"],
  ["not_ready", "Not ready", "warning"],
  ["error", "Error", "danger"],
  ["unreachable", "Unreachable", "danger"],
  ["checking", "Checking…", "neutral"],
];

describe("status-to-label mapping", () => {
  it("covers every state", () => {
    expect(Object.keys(STATUS_META).sort()).toEqual(EXPECTED.map(([state]) => state).sort());
  });

  it("gives every tone more than colour: distinct icons for distinct tones", () => {
    const iconsByTone = new Map<string, Set<unknown>>();
    for (const meta of Object.values(STATUS_META)) {
      iconsByTone.set(meta.tone, (iconsByTone.get(meta.tone) ?? new Set()).add(meta.icon));
    }
    const allIcons = [...iconsByTone.values()].flatMap((icons) => [...icons]);
    expect(new Set(allIcons).size).toBe(allIcons.length);
  });

  it.each(EXPECTED)("renders %s with label %j, tone %s and a decorative icon", (state, label, tone) => {
    expect(getStatusMeta(state)).toMatchObject({ label, tone });

    const { container } = render(<StatusBadge state={state} />);

    const badge = screen.getByText(label);
    expect(badge).toBeVisible();
    expect(badge).toHaveAttribute("data-state", state);
    expect(badge).toHaveAttribute("data-tone", tone);
    const icon = container.querySelector("svg");
    expect(icon).not.toBeNull();
    expect(icon).toHaveAttribute("aria-hidden", "true");
  });

  it("allows the label to be overridden while keeping the state", () => {
    render(<StatusBadge state="ok" label="Live" />);
    expect(screen.getByText("Live")).toHaveAttribute("data-state", "ok");
    expect(screen.queryByText("OK")).toBeNull();
  });

  it("spins only the checking icon", () => {
    const { container, rerender } = render(<StatusBadge state="checking" />);
    expect(container.querySelector("svg")).toHaveClass("animate-spin");
    rerender(<StatusBadge state="ready" />);
    expect(container.querySelector("svg")).not.toHaveClass("animate-spin");
  });
});
