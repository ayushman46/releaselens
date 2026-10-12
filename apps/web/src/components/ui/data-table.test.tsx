import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DataTable, sortRows, type DataTableColumn } from "@/components/ui/data-table";

interface Row {
  id: string;
  service: string;
  p95: number | null;
}

const ROWS: Row[] = [
  { id: "a", service: "orders", p95: 120 },
  { id: "b", service: "fraud", p95: null },
  { id: "c", service: "payment", p95: 40 },
  { id: "d", service: "gateway", p95: 75 },
];

const COLUMNS: DataTableColumn<Row>[] = [
  { id: "service", header: "Service", cell: (r) => r.service, sortValue: (r) => r.service, rowHeader: true },
  { id: "p95", header: "p95 latency", cell: (r) => (r.p95 === null ? "—" : `${r.p95} ms`), sortValue: (r) => r.p95, align: "right" },
  { id: "note", header: "Note", cell: () => "" },
];

function bodyOrder(): string[] {
  const [, body] = screen.getAllByRole("rowgroup");
  return within(body!)
    .getAllByRole("row")
    .map((row) => within(row).getByRole("rowheader").textContent ?? "");
}

function renderTable(props: Partial<Parameters<typeof DataTable<Row>>[0]> = {}) {
  return render(
    <DataTable<Row> caption="Services" columns={COLUMNS} rows={ROWS} getRowId={(r) => r.id} {...props} />,
  );
}

describe("DataTable", () => {
  it("renders a captioned table with column headers", () => {
    renderTable();
    expect(screen.getByRole("table", { name: "Services" })).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "Service",
      "p95 latency",
      "Note",
    ]);
  });

  it("sorts by a column on click and toggles direction, updating aria-sort", () => {
    renderTable();
    const header = screen.getByRole("columnheader", { name: /Service/ });
    expect(header).not.toHaveAttribute("aria-sort");

    fireEvent.click(within(header).getByRole("button", { name: /Service/ }));
    expect(header).toHaveAttribute("aria-sort", "ascending");
    expect(bodyOrder()).toEqual(["fraud", "gateway", "orders", "payment"]);
    expect(screen.getByText("Sorted by Service, ascending")).toBeInTheDocument();

    fireEvent.click(within(header).getByRole("button", { name: /Service/ }));
    expect(header).toHaveAttribute("aria-sort", "descending");
    expect(bodyOrder()).toEqual(["payment", "orders", "gateway", "fraud"]);
  });

  it("keeps missing values last in both directions", () => {
    renderTable({ initialSort: { columnId: "p95", direction: "ascending" } });
    expect(bodyOrder()).toEqual(["payment", "gateway", "orders", "fraud"]);
    fireEvent.click(screen.getByRole("button", { name: /p95 latency/ }));
    expect(bodyOrder()).toEqual(["orders", "gateway", "payment", "fraud"]);
  });

  it("only renders sort buttons for sortable columns (native buttons: Enter and Space work)", () => {
    renderTable();
    expect(screen.getAllByRole("button").map((b) => b.textContent)).toEqual(["Service", "p95 latency"]);
    expect(within(screen.getByRole("columnheader", { name: "Note" })).queryByRole("button")).toBeNull();
  });

  it("moves focus between rows with the arrow keys, Home and End (roving tabindex)", () => {
    renderTable({ initialSort: { columnId: "service", direction: "ascending" } });
    const [, body] = screen.getAllByRole("rowgroup");
    const rows = within(body!).getAllByRole("row");
    expect(rows.map((r) => r.tabIndex)).toEqual([0, -1, -1, -1]);

    rows[0]!.focus();
    fireEvent.keyDown(rows[0]!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(rows[1]);
    expect(rows.map((r) => r.tabIndex)).toEqual([-1, 0, -1, -1]);

    fireEvent.keyDown(rows[1]!, { key: "End" });
    expect(document.activeElement).toBe(rows[3]);
    fireEvent.keyDown(rows[3]!, { key: "ArrowDown" });
    expect(document.activeElement).toBe(rows[3]);
    fireEvent.keyDown(rows[3]!, { key: "Home" });
    expect(document.activeElement).toBe(rows[0]);
    fireEvent.keyDown(rows[0]!, { key: "ArrowUp" });
    expect(document.activeElement).toBe(rows[0]);
  });

  it("activates a row with Enter", () => {
    const onRowActivate = vi.fn();
    renderTable({ onRowActivate, initialSort: { columnId: "service", direction: "ascending" } });
    const [, body] = screen.getAllByRole("rowgroup");
    const first = within(body!).getAllByRole("row")[0]!;
    first.focus();
    fireEvent.keyDown(first, { key: "Enter" });
    expect(onRowActivate).toHaveBeenCalledWith(expect.objectContaining({ service: "fraud" }));
  });

  it("renders the empty state inside the table when there are no rows", () => {
    render(
      <DataTable<Row>
        caption="Services"
        columns={COLUMNS}
        rows={[]}
        getRowId={(r) => r.id}
        emptyState={<p>No services reporting</p>}
      />,
    );
    const cell = screen.getByText("No services reporting").closest("td");
    expect(cell).toHaveAttribute("colspan", "3");
    expect(screen.getAllByRole("columnheader")).toHaveLength(3);
  });
});

describe("sortRows", () => {
  it("is stable for equal keys", () => {
    const rows = [
      { id: "1", k: 1 },
      { id: "2", k: 0 },
      { id: "3", k: 1 },
    ];
    expect(sortRows(rows, (r) => r.k, "ascending").map((r) => r.id)).toEqual(["2", "1", "3"]);
    expect(sortRows(rows, (r) => r.k, "descending").map((r) => r.id)).toEqual(["1", "3", "2"]);
  });

  it("compares strings naturally", () => {
    const rows = [{ v: "svc-10" }, { v: "svc-2" }, { v: "Svc-1" }];
    expect(sortRows(rows, (r) => r.v, "ascending").map((r) => r.v)).toEqual(["Svc-1", "svc-2", "svc-10"]);
  });
});
