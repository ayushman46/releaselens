import { afterEach, describe, expect, it, vi } from "vitest";

import { deleteView, loadViews, saveView, storageKey } from "@/lib/saved-views";

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("saved views", () => {
  const key = storageKey("deployments", "org-1");

  it("saves, replaces by name, drops empty filters and deletes", () => {
    expect(loadViews(key)).toEqual([]);
    saveView(key, { name: "Prod payments", filters: { service: "payment", environment: "prod" } });
    saveView(key, { name: "Staging", filters: { environment: "staging", service: " " } });
    const views = saveView(key, { name: "Prod payments", filters: { service: "payment", environment: "production" } });
    expect(views).toEqual([
      { name: "Prod payments", filters: { service: "payment", environment: "production" } },
      { name: "Staging", filters: { environment: "staging" } },
    ]);
    expect(deleteView(key, "Staging").map((v) => v.name)).toEqual(["Prod payments"]);
  });

  it("is per organization", () => {
    saveView(key, { name: "A", filters: {} });
    expect(loadViews(storageKey("deployments", "org-2"))).toEqual([]);
  });

  it("ignores corrupt data and never throws when storage is unavailable", () => {
    localStorage.setItem(key, "{not json");
    expect(loadViews(key)).toEqual([]);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    expect(() => saveView(key, { name: "x", filters: {} })).not.toThrow();
    expect(loadViews(key)).toEqual([]);
  });
});
