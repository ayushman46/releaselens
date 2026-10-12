import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { makeQueryClient } from "@/components/providers";
import { StatusDashboard } from "@/components/status/status-dashboard";

function renderDashboard() {
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <StatusDashboard baseUrl="http://api.test" />
    </QueryClientProvider>,
  );
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("StatusDashboard", () => {
  it("lists each dependency with status label and latency", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        url.endsWith("/healthz")
          ? json({ status: "ok" })
          : json(
              {
                status: "not_ready",
                checks: {
                  kafka: { status: "error", latency_ms: 2000.4, error: "timed out after 2.0s" },
                  postgres: { status: "ok", latency_ms: 3.2 },
                },
              },
              503,
            ),
      ),
    );

    renderDashboard();

    // Placeholder rows render while the first probe is in flight; wait for real data.
    await screen.findByText("3.2 ms");
    const postgres = screen.getByRole("rowheader", { name: "PostgreSQL" }).closest("tr")!;
    expect(within(postgres).getByText("OK")).toBeInTheDocument();
    expect(within(postgres).getByText("3.2 ms")).toBeInTheDocument();

    const kafka = screen.getByRole("rowheader", { name: /Kafka/ }).closest("tr")!;
    expect(within(kafka).getByText("Error")).toBeInTheDocument();
    expect(within(kafka).getByText("2,000 ms")).toBeInTheDocument();
    expect(within(kafka).getByText("timed out after 2.0s")).toBeInTheDocument();

    expect(screen.getAllByText("Not ready").length).toBeGreaterThan(0);
  });

  it("shows an error state with a working retry when the API is unreachable", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);

    renderDashboard();

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("heading", { name: "API unreachable" })).toBeInTheDocument();
    expect(screen.getAllByText("Unreachable").length).toBeGreaterThan(0);

    const callsBefore = fetchMock.mock.calls.length;
    fireEvent.click(within(alert).getByRole("button", { name: /retry/i }));
    await vi.waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore));
  });
});
