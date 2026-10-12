import { render, screen } from "@testing-library/react";
import { Rocket } from "lucide-react";
import { describe, expect, it } from "vitest";

import { StatusPill } from "@/components/rl/status-pill";
import { TONE_META, TONES } from "@/lib/tones";

describe("StatusPill", () => {
  it.each(TONES)("renders the %s tone with visible text and a decorative icon", (tone) => {
    const { container } = render(<StatusPill tone={tone} label={`Label for ${tone}`} />);

    const pill = container.querySelector('[data-slot="status-pill"]');
    expect(pill).toHaveAttribute("data-tone", tone);
    expect(screen.getByText(`Label for ${tone}`)).toBeVisible();

    const icons = pill!.querySelectorAll("svg");
    expect(icons).toHaveLength(1);
    expect(icons[0]).toHaveAttribute("aria-hidden", "true");
  });

  it.each(TONES)("never renders colour-only: a blank %s label falls back to the tone's own text", (tone) => {
    const { container } = render(<StatusPill tone={tone} label="   " />);
    const pill = container.querySelector('[data-slot="status-pill"]')!;
    expect(pill.textContent?.trim()).toBe(TONE_META[tone].label);
    expect(pill.textContent?.trim().length).toBeGreaterThan(0);
  });

  it("gives every tone a distinct icon, so tones differ by more than colour", () => {
    const icons = TONES.map((tone) => TONE_META[tone].icon);
    expect(new Set(icons).size).toBe(TONES.length);
  });

  it("lets callers swap the icon but never drop it", () => {
    const { container } = render(<StatusPill tone="info" label="Deployed" icon={Rocket} />);
    expect(container.querySelectorAll("svg")).toHaveLength(1);
    expect(screen.getByText("Deployed")).toBeVisible();
  });

  it("adds screen-reader context before the label", () => {
    render(<StatusPill tone="critical" label="Critical" srPrefix="Severity:" />);
    const pill = screen.getByText("Critical").closest('[data-slot="status-pill"]')!;
    expect(pill.textContent).toBe("Severity: Critical");
  });
});
