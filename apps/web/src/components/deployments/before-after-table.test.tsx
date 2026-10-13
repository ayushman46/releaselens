import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { BeforeAfterTable } from "@/components/deployments/before-after-table";
import { analysisResult, paymentEvidence } from "@/test/analysis-fixture";

describe("BeforeAfterTable", () => {
  it("renders before/after, the change, its CI and sample sizes per metric", () => {
    render(<BeforeAfterTable services={analysisResult.services} selected={paymentEvidence} origin="payment" />);
    const table = screen.getByRole("table");
    const p95 = within(table).getByRole("rowheader", { name: /p95 latency/ }).closest("tr")!;
    const cells = within(p95).getAllByRole("cell").map((c) => c.textContent);
    expect(cells).toEqual([
      "92.5 ms",
      "343 ms",
      "Increase: +250 ms (+270.3%)",
      "95% CI +210 ms to +290 ms",
      "n = 6,012 → 4,980 requests",
      "All passed: regressed",
    ]);
    const err = within(table).getByRole("rowheader", { name: /Error rate/ }).closest("tr")!;
    expect(err).toHaveTextContent("0.20%");
    expect(err).toHaveTextContent("3.69%");
    expect(err).toHaveTextContent("+3.49 pp");
    expect(err).toHaveTextContent("12/6,012 → 184/4,980 errors");
  });

  it("shows the service's verdict and severity as text", () => {
    render(<BeforeAfterTable services={analysisResult.services} selected={paymentEvidence} />);
    expect(screen.getByText("Regressed")).toBeInTheDocument();
    expect(screen.getByText("High · 59")).toBeInTheDocument();
  });

  it("switches service from a labelled select that marks the probable origin", () => {
    const onSelect = vi.fn();
    render(<BeforeAfterTable services={analysisResult.services} selected={paymentEvidence} onSelect={onSelect} origin="payment" />);
    const select = screen.getByLabelText("Service");
    expect(within(select).getByRole("option", { name: "payment (probable origin)" })).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "orders" } });
    expect(onSelect).toHaveBeenCalledWith("orders");
  });

  it("says why nothing was tested when the sample gate failed", () => {
    const thin = { ...paymentEvidence, status: "INSUFFICIENT_DATA" as const, sample_gate: { baseline_requests: 120, post_requests: 80, min_requests: 200, passed: false } };
    render(<BeforeAfterTable services={[thin]} selected={thin} />);
    expect(screen.getByText(/Insufficient data: 120 requests before and 80 after, below the minimum of 200/)).toBeInTheDocument();
  });
});
