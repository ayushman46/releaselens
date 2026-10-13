import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ciStatusMeta } from "@/components/risk/ci-run-signal";
import { shapRows } from "@/components/risk/risk-views";
import { ShapBars } from "@/components/risk/shap-bars";

const up = { feature: "prev_build_failed", features: ["prev_build_failed"], label: "Previous finished build failed", value: 1, contribution: 0.55, text: "Previous finished build failed: yes" };
const down = { feature: "builds_since_last_failure", features: ["builds_since_last_failure"], label: "Builds since the last failure", value: 149, contribution: -0.88, text: "in the top 1%" };

describe("SHAP explanations", () => {
  it("states each factor's direction in words, not only by bar side or colour", () => {
    render(<ShapBars increasing={[up]} reducing={[down]} />);
    const list = screen.getByRole("list", { name: /SHAP contributions/ });
    expect(list).toHaveTextContent("raised +0.55");
    expect(list).toHaveTextContent("lowered −0.88");
    expect(list).toHaveTextContent("Previous finished build failed= 1");
  });

  it("says so when there is no explanation", () => {
    render(<ShapBars increasing={[]} reducing={[]} />);
    expect(screen.getByText(/no explanation/)).toBeInTheDocument();
  });

  it("orders the full SHAP vector by magnitude and labels known features", () => {
    const rows = shapRows({
      shap: { values: { prev_build_failed: 0.55, builds_since_last_failure: -0.88, team_size: 0.01 }, unit: "log-odds", base_value: -1 },
      features: { prev_build_failed: 1, builds_since_last_failure: 149, team_size: 9 },
      top_increasing: [up],
      top_reducing: [down],
    });
    expect(rows.map((r) => r.label)).toEqual(["Builds since the last failure", "Previous finished build failed", "team_size"]);
    expect(rows[2]?.value).toBe(9);
  });
});

describe("CI result signal", () => {
  it("maps run statuses to text labels", () => {
    expect(ciStatusMeta("failed")).toEqual({ tone: "critical", label: "CI failed" });
    expect(ciStatusMeta("passed").label).toBe("CI passed");
    expect(ciStatusMeta("weird")).toEqual({ tone: "unknown", label: "CI weird" });
  });
});
