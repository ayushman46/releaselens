import { describe, expect, it } from "vitest";

import { DEFAULT_API_URL, resolveApiBaseUrl } from "@/lib/env";

describe("resolveApiBaseUrl", () => {
  it.each([undefined, "", "   "])("falls back to the local default for %j", (raw) => {
    expect(resolveApiBaseUrl(raw)).toBe(DEFAULT_API_URL);
  });

  it("falls back for values that are not absolute http(s) URLs", () => {
    expect(resolveApiBaseUrl("localhost:28000")).toBe(DEFAULT_API_URL);
    expect(resolveApiBaseUrl("ftp://api.example.com")).toBe(DEFAULT_API_URL);
  });

  it("trims whitespace and trailing slashes", () => {
    expect(resolveApiBaseUrl(" https://api.example.com/ ")).toBe("https://api.example.com");
    expect(resolveApiBaseUrl("http://localhost:28000/api//")).toBe("http://localhost:28000/api");
  });
});
