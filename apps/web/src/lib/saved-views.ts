import { safeStorage } from "@/lib/storage";

/** A named set of list filters, kept in this browser only (localStorage, failures ignored). */
export interface SavedView {
  name: string;
  filters: Record<string, string>;
}

const MAX_VIEWS = 20;

function isSavedViewList(value: unknown): value is SavedView[] {
  return (
    Array.isArray(value) &&
    value.every(
      (v) =>
        typeof v === "object" &&
        v !== null &&
        typeof (v as SavedView).name === "string" &&
        typeof (v as SavedView).filters === "object" &&
        (v as SavedView).filters !== null &&
        Object.values((v as SavedView).filters).every((f) => typeof f === "string"),
    )
  );
}

export function storageKey(scope: string, orgId: string | undefined): string {
  return `rl.savedViews.${scope}.${orgId ?? "none"}`;
}

export function loadViews(key: string): SavedView[] {
  return safeStorage.getJson(key, isSavedViewList) ?? [];
}

/** Adds or replaces (same name) a view; returns the new list, or the old one if storage failed. */
export function saveView(key: string, view: SavedView): SavedView[] {
  const name = view.name.trim();
  if (!name) return loadViews(key);
  const filters = Object.fromEntries(Object.entries(view.filters).filter(([, v]) => v.trim() !== ""));
  const next = [{ name, filters }, ...loadViews(key).filter((v) => v.name !== name)].slice(0, MAX_VIEWS);
  return safeStorage.setJson(key, next) ? next : loadViews(key);
}

export function deleteView(key: string, name: string): SavedView[] {
  const next = loadViews(key).filter((v) => v.name !== name);
  return safeStorage.setJson(key, next) ? next : loadViews(key);
}
