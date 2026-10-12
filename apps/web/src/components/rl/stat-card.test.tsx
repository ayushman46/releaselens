import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatCard } from "@/components/rl/stat-card";

describe("StatCard", () => {
  it("renders a dash and explains why when the value is missing (never a fake zero)", () => {
    render(<StatCard label="Deployments, last 7 days" missingReason="No deployment events yet." />);
    expect(screen.getByText("—")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("No data.")).toBeInTheDocument();
    expect(screen.queryByText("0")).toBeNull();

    const why = screen.getByRole("button", { name: "Why is Deployments, last 7 days empty?" });
    expect(why).toHaveAccessibleDescription("No deployment events yet.");

    const tooltip = screen.getByRole("tooltip", { hidden: true });
    expect(tooltip).not.toBeVisible();
    // Keyboard focus opens it immediately (hover waits a moment).
    act(() => why.focus());
    expect(screen.getByRole("tooltip")).toBeVisible();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.getByRole("tooltip", { hidden: true })).not.toBeVisible();
  });

  it("shows the value, the delta with a direction word, and the sample size", () => {
    render(
      <StatCard
        label="p95 latency"
        value="212"
        unit="ms"
        delta={{ value: "+18%", direction: "up", sentiment: "negative", label: "vs baseline" }}
        sampleSize={4812}
        sampleLabel="requests"
        missingReason="unused"
      />,
    );
    expect(screen.getByText("212")).toBeVisible();
    expect(screen.getByText("ms")).toBeVisible();
    expect(screen.getByText("Up")).toBeInTheDocument();
    expect(screen.getByText("+18%")).toBeVisible();
    expect(screen.getByText("vs baseline")).toBeVisible();
    expect(screen.getByText("n = 4,812 requests")).toBeVisible();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
