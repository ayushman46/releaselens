import { describe, expect, it } from "vitest";

import { layeredLayout } from "@/lib/graph-layout";

describe("layeredLayout", () => {
  it("puts callers left of callees by longest path", () => {
    const pos = layeredLayout(["gateway", "orders", "payment", "fraud", "inventory"], [
      { source: "gateway", target: "orders" },
      { source: "orders", target: "payment" },
      { source: "orders", target: "inventory" },
      { source: "payment", target: "fraud" },
      { source: "gateway", target: "fraud" },
    ]);
    const x = (id: string) => pos.get(id)!.x;
    expect(x("gateway")).toBe(0);
    expect(x("orders")).toBeGreaterThan(x("gateway"));
    expect(x("fraud")).toBeGreaterThan(x("payment"));
    expect(x("inventory")).toBe(x("payment"));
  });

  it("survives cycles, self-calls and unknown edge ends", () => {
    const pos = layeredLayout(["a", "b"], [
      { source: "a", target: "b" },
      { source: "b", target: "a" },
      { source: "a", target: "a" },
      { source: "a", target: "ghost" },
    ]);
    expect(pos.size).toBe(2);
    expect(pos.get("a")!.x).not.toBe(pos.get("b")!.x);
  });
});
