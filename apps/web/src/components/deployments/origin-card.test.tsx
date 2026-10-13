import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { OriginCard } from "@/components/deployments/origin-card";
import { originView } from "@/lib/analysis";
import { analysisOut, analysisResult } from "@/test/analysis-fixture";

describe("OriginCard", () => {
  it("says 'probable origin' (never 'root cause') and shows the service, confidence and reasoning bullets", () => {
    const { container } = render(<OriginCard origin={originView(analysisResult, analysisOut)} />);
    const card = screen.getByRole("region", { name: "Probable origin" });
    expect(within(card).getByRole("heading", { name: "Probable origin" })).toBeInTheDocument();
    expect(within(card).getByTestId("probable-origin")).toHaveTextContent("payment");
    expect(within(card).getByText("High confidence")).toBeVisible();
    expect(within(card).getByText(/A probable origin, not a proven cause/)).toBeInTheDocument();
    const bullets = within(card).getAllByRole("listitem").map((li) => li.textContent);
    expect(bullets).toEqual([
      "payment was the deployed service",
      "payment's own time (self-time) p95 regressed",
      "payment originates the new errors",
    ]);
    expect(container.textContent).not.toMatch(/root cause/i);
  });

  it("names no service when confidence is LOW, and says why", () => {
    const low = { ...analysisResult, triage: { ...analysisResult.triage!, confidence: "LOW" as const, probable_origin: null } };
    render(<OriginCard origin={originView(low, { ...analysisOut, probable_origin: null, confidence: "LOW" })} />);
    expect(screen.queryByTestId("probable-origin")).toBeNull();
    expect(screen.getByText("Low confidence")).toBeInTheDocument();
    expect(screen.getByText(/No probable origin named/)).toBeInTheDocument();
    expect(screen.getByText(/top-ranked candidate is/)).toHaveTextContent("payment");
  });

  it("explains an empty card when nothing regressed", () => {
    render(<OriginCard origin={null} />);
    expect(screen.getByText(/nothing regressed in this analysis/)).toBeInTheDocument();
  });
});
