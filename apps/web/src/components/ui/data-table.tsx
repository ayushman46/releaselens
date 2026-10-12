"use client";

import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import { useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";

import { cn } from "@/lib/utils";

export type SortDirection = "ascending" | "descending";
export type SortValue = string | number | Date | null | undefined;

export interface DataTableColumn<T> {
  id: string;
  header: string;
  cell: (row: T) => ReactNode;
  /** Makes the column sortable. Nullish values always sort last. */
  sortValue?: (row: T) => SortValue;
  align?: "left" | "right";
  /** Render this column's cells as row headers (`<th scope="row">`). Use for the name column. */
  rowHeader?: boolean;
  className?: string;
}

export interface SortState {
  columnId: string;
  direction: SortDirection;
}

function compare(a: SortValue, b: SortValue): number {
  const aMissing = a === null || a === undefined;
  const bMissing = b === null || b === undefined;
  if (aMissing || bMissing) return aMissing === bMissing ? 0 : aMissing ? 1 : -1;
  if (a instanceof Date && b instanceof Date) return a.getTime() - b.getTime();
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

/** Stable sort; nullish values stay at the end in both directions. */
export function sortRows<T>(
  rows: readonly T[],
  getValue: (row: T) => SortValue,
  direction: SortDirection,
): T[] {
  const sign = direction === "ascending" ? 1 : -1;
  return rows
    .map((row, index) => ({ row, index, value: getValue(row) }))
    .sort((x, y) => {
      const xMissing = x.value === null || x.value === undefined;
      const yMissing = y.value === null || y.value === undefined;
      if (xMissing !== yMissing) return xMissing ? 1 : -1;
      return sign * compare(x.value, y.value) || x.index - y.index;
    })
    .map(({ row }) => row);
}

/**
 * A small accessible data table.
 * - Sortable columns render their header as a button; `aria-sort` marks the sorted column and a
 *   polite live region announces changes.
 * - Rows are keyboard navigable with a roving tabindex: Tab enters the table body once, ↑/↓ move
 *   between rows, Home/End jump, Enter activates the row (`onRowActivate`).
 * - The header sticks to the top of the scroll container (`maxHeight`).
 * - With no rows, `emptyState` is rendered in place of the body.
 */
export function DataTable<T>({
  columns,
  rows,
  getRowId,
  caption,
  captionVisible = false,
  initialSort,
  emptyState,
  onRowActivate,
  maxHeight = "max-h-[32rem]",
  className,
}: {
  columns: DataTableColumn<T>[];
  rows: readonly T[];
  getRowId: (row: T) => string;
  /** Describes the table for assistive tech (visually hidden unless `captionVisible`). */
  caption: string;
  captionVisible?: boolean;
  initialSort?: SortState;
  emptyState?: ReactNode;
  onRowActivate?: (row: T) => void;
  /** Tailwind max-height class for the scroll container (the header sticks inside it). */
  maxHeight?: string;
  className?: string;
}) {
  const [sort, setSort] = useState<SortState | undefined>(initialSort);
  const [activeRowId, setActiveRowId] = useState<string | undefined>(undefined);
  const bodyRef = useRef<HTMLTableSectionElement>(null);

  const sortedRows = useMemo(() => {
    const column = sort ? columns.find((c) => c.id === sort.columnId) : undefined;
    if (!sort || !column?.sortValue) return [...rows];
    return sortRows(rows, column.sortValue, sort.direction);
  }, [rows, columns, sort]);

  const focusRowId =
    activeRowId && sortedRows.some((r) => getRowId(r) === activeRowId)
      ? activeRowId
      : sortedRows[0]
        ? getRowId(sortedRows[0])
        : undefined;

  const toggleSort = (columnId: string) => {
    setSort((current) =>
      current?.columnId === columnId
        ? { columnId, direction: current.direction === "ascending" ? "descending" : "ascending" }
        : { columnId, direction: "ascending" },
    );
  };

  const focusRowAt = (index: number) => {
    const row = sortedRows[index];
    if (!row) return;
    setActiveRowId(getRowId(row));
    // Body rows render in sortedRows order.
    bodyRef.current?.rows[index]?.focus();
  };

  const onRowKeyDown = (event: KeyboardEvent<HTMLTableRowElement>, index: number, row: T) => {
    const last = sortedRows.length - 1;
    const page = 10;
    const target =
      event.key === "ArrowDown"
        ? Math.min(index + 1, last)
        : event.key === "ArrowUp"
          ? Math.max(index - 1, 0)
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? last
              : event.key === "PageDown"
                ? Math.min(index + page, last)
                : event.key === "PageUp"
                  ? Math.max(index - page, 0)
                  : undefined;
    if (target !== undefined) {
      event.preventDefault();
      focusRowAt(target);
      return;
    }
    if ((event.key === "Enter" || event.key === " ") && onRowActivate && event.target === event.currentTarget) {
      event.preventDefault();
      onRowActivate(row);
    }
  };

  const sortedColumn = sort ? columns.find((c) => c.id === sort.columnId) : undefined;

  return (
    <div data-slot="data-table" className={cn("relative rounded-lg border bg-panel", className)}>
      <div className={cn("overflow-auto rounded-lg", maxHeight)}>
        <table className="w-full border-separate border-spacing-0 text-sm">
          <caption
            className={cn(
              captionVisible ? "px-4 py-3 text-left text-sm font-medium text-foreground" : "sr-only",
            )}
          >
            {caption}
          </caption>
          <thead>
            <tr>
              {columns.map((column) => {
                const isSorted = sort?.columnId === column.id;
                const SortIcon = !isSorted
                  ? ChevronsUpDown
                  : sort.direction === "ascending"
                    ? ArrowUp
                    : ArrowDown;
                return (
                  <th
                    key={column.id}
                    scope="col"
                    aria-sort={isSorted ? sort.direction : undefined}
                    className={cn(
                      "sticky top-0 z-10 h-9 border-b bg-panel px-4 text-xs font-medium whitespace-nowrap text-muted-foreground",
                      column.align === "right" ? "text-right" : "text-left",
                      column.className,
                    )}
                  >
                    {column.sortValue ? (
                      <button
                        type="button"
                        onClick={() => toggleSort(column.id)}
                        className={cn(
                          "-mx-1.5 inline-flex h-7 items-center gap-1 rounded-md px-1.5 hover:bg-subtle hover:text-foreground",
                          column.align === "right" && "flex-row-reverse",
                          isSorted && "text-foreground",
                        )}
                      >
                        {column.header}
                        <SortIcon
                          aria-hidden="true"
                          className={cn("size-3.5", !isSorted && "text-muted-foreground/70")}
                        />
                      </button>
                    ) : (
                      column.header
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody ref={bodyRef}>
            {sortedRows.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="px-4 py-10">
                  {emptyState ?? <p className="text-center text-sm text-muted-foreground">No rows.</p>}
                </td>
              </tr>
            ) : (
              sortedRows.map((row, index) => {
                const id = getRowId(row);
                return (
                  <tr
                    key={id}
                    data-row-id={id}
                    tabIndex={id === focusRowId ? 0 : -1}
                    onKeyDown={(event) => onRowKeyDown(event, index, row)}
                    onFocus={() => setActiveRowId(id)}
                    onClick={onRowActivate ? () => onRowActivate(row) : undefined}
                    className={cn(
                      "group transition-colors -outline-offset-2 hover:bg-subtle/60 focus-visible:bg-subtle/60",
                      onRowActivate && "cursor-pointer",
                    )}
                  >
                    {columns.map((column) => {
                      const Cell = column.rowHeader ? "th" : "td";
                      return (
                        <Cell
                          key={column.id}
                          scope={column.rowHeader ? "row" : undefined}
                          className={cn(
                            "h-11 border-b px-4 align-middle group-last:border-b-0",
                            column.rowHeader ? "font-medium text-foreground" : "text-foreground-secondary",
                            column.align === "right" ? "text-right tabular-nums" : "text-left",
                            column.className,
                          )}
                        >
                          {column.cell(row)}
                        </Cell>
                      );
                    })}
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      <p aria-live="polite" className="sr-only">
        {sortedColumn && sort ? `Sorted by ${sortedColumn.header}, ${sort.direction}` : ""}
      </p>
    </div>
  );
}
