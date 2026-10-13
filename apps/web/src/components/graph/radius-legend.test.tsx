import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RadiusList } from "@/components/graph/blast-radius";
import { RadiusLegend, radiusNodeClasses } from "@/components/graph/radius-legend";
import { RADIUS_ROLE_META, radiusGraph, type RadiusRole } from "@/lib/analysis";
import { analysisResult } from "@/test/analysis-fixture";

const ROLES: RadiusRole[] = ["origin", "affected", "healthy", "unknown", "outside"];

describe("blast radius legend", () => {
  it("labels every role in text, with an icon and a border style (never colour alone)", () => {
    render(<RadiusLegend />);
    const legend = screen.getByRole("list", { name: "Legend" });
    const items = within(legend).getAllByRole("listitem");
    expect(items.map((li) => li.textContent?.split(":")[0])).toEqual([
      "Probable origin",
      "Affected",
      "Healthy",
      "Insufficient data",
      "Not in radius",
    ]);
    for (const item of items) {
      expect(item.querySelector("svg")).not.toBeNull();
      expect(item.querySelector("svg")?.closest('[aria-hidden="true"]')).not.toBeNull();
    }
  });

  it("gives each role a distinct border style or icon so roles differ by shape", () => {
    const borders = new Set(ROLES.filter((r) => r !== "outside").map((r) => RADIUS_ROLE_META[r].border));
    expect(borders.size).toBe(4);
    expect(radiusNodeClasses("origin")).toContain("border-double");
    expect(radiusNodeClasses("affected")).toContain("border-solid");
    expect(radiusNodeClasses("healthy")).toContain("border-dashed");
    expect(radiusNodeClasses("unknown")).toContain("border-dotted");
  });

  it("accepts context wording, e.g. on the service map", () => {
    render(<RadiusLegend roles={["outside"]} labels={{ outside: "Not in the latest analysis" }} />);
    expect(screen.getByText("Not in the latest analysis")).toBeInTheDocument();
  });

  it("lists the same graph as text for screen readers", () => {
    render(<RadiusList graph={radiusGraph(analysisResult)!} />);
    expect(screen.getByText("Probable origin").nextSibling).toHaveTextContent("payment");
    expect(screen.getByText("Affected").nextSibling).toHaveTextContent("orders (upstream, 1 hop), gateway (upstream, 2 hops)");
    expect(screen.getByText("Not in radius").nextSibling).toHaveTextContent("inventory");
  });
});
