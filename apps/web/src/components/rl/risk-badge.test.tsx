import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RiskBadge } from "@/components/rl/risk-badge";
import { SeverityBadge } from "@/components/rl/severity-badge";
import {
  normalizeScore,
  RISK_BAND_META,
  RISK_BANDS,
  SEVERITY_BANDS,
  SEVERITY_META,
  type RiskBand,
  type SeverityBand,
} from "@/lib/tones";

function badge(container: HTMLElement, slot: string) {
  return container.querySelector(`[data-slot="${slot}"]`)!;
}

describe("RiskBadge", () => {
  it.each<[RiskBand, string, string]>([
    ["LOW", "healthy", "Low"],
    ["MEDIUM", "warning", "Medium"],
    ["HIGH", "high", "High"],
    ["CRITICAL", "critical", "Critical"],
  ])("maps band %s to the %s tone and the label %j", (band, tone, label) => {
    expect(RISK_BAND_META[band]).toMatchObject({ tone, label });

    const { container } = render(<RiskBadge score={42} band={band} />);
    const el = badge(container, "risk-badge");
    expect(el).toHaveAttribute("data-tone", tone);
    expect(el).toHaveAttribute("data-band", band);
    expect(el).toHaveTextContent(label);
    expect(el).toHaveTextContent("42");
    expect(el.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("covers every band with a distinct icon (signal bars rise with the band)", () => {
    expect(Object.keys(RISK_BAND_META).sort()).toEqual([...RISK_BANDS].sort());
    const icons = RISK_BANDS.map((band) => RISK_BAND_META[band].icon);
    expect(new Set(icons).size).toBe(RISK_BANDS.length);
  });

  it("reads as a score out of 100 of CI failure risk, not a probability", () => {
    const { container } = render(<RiskBadge score={78} band="HIGH" />);
    expect(badge(container, "risk-badge").textContent).toBe("CI failure risk: 78 out of 100,·High");
    expect(container.textContent).not.toMatch(/%|probab|outage/i);
  });

  it("rounds and clamps scores into 0–100", () => {
    expect(normalizeScore(77.6)).toBe(78);
    expect(normalizeScore(-3)).toBe(0);
    expect(normalizeScore(140)).toBe(100);
    expect(normalizeScore(Number.NaN)).toBe(0);
    const { container } = render(<RiskBadge score={101.2} band="CRITICAL" />);
    expect(badge(container, "risk-badge")).toHaveTextContent("100");
  });

  it.each([
    [null, "HIGH"],
    [55, null],
    [undefined, undefined],
  ] as const)("shows 'Not scored' instead of guessing when score=%s band=%s", (score, band) => {
    const { container } = render(<RiskBadge score={score} band={band} />);
    const el = badge(container, "risk-badge");
    expect(el).toHaveAttribute("data-tone", "unknown");
    expect(el).toHaveTextContent("Not scored");
  });
});

describe("SeverityBadge", () => {
  it.each<[SeverityBand, string, string]>([
    ["HEALTHY", "healthy", "Healthy"],
    ["WARNING", "warning", "Warning"],
    ["HIGH", "high", "High"],
    ["CRITICAL", "critical", "Critical"],
  ])("maps %s to the %s tone", (severity, tone, label) => {
    expect(SEVERITY_META[severity]).toMatchObject({ tone, label });
    const { container } = render(<SeverityBadge severity={severity} />);
    const el = badge(container, "status-pill");
    expect(el).toHaveAttribute("data-tone", tone);
    expect(el.textContent).toBe(`Severity: ${label}`);
  });

  it("covers every severity band", () => {
    expect(Object.keys(SEVERITY_META).sort()).toEqual([...SEVERITY_BANDS].sort());
  });

  it("appends the deterministic score when given", () => {
    const { container } = render(<SeverityBadge severity="HIGH" score={71.4} />);
    expect(badge(container, "status-pill")).toHaveTextContent("High · 71");
  });

  it("says 'Insufficient data' when the gates could not decide", () => {
    const { container } = render(<SeverityBadge severity={null} />);
    const el = badge(container, "status-pill");
    expect(el).toHaveAttribute("data-tone", "unknown");
    expect(el).toHaveTextContent("Insufficient data");
  });
});
