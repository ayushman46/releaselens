/** Liveness for the container healthcheck: no dependencies checked (CONVENTIONS.md §4). */
export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json({ status: "ok" }, { headers: { "cache-control": "no-store" } });
}
