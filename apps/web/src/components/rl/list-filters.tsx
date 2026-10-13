"use client";

import { Bookmark, Filter, Trash2, X } from "lucide-react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useId, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/field";
import { deleteView, loadViews, saveView, type SavedView } from "@/lib/saved-views";

export interface FilterField {
  key: string;
  label: string;
  placeholder?: string;
  /** Values seen in loaded data, offered as suggestions. */
  suggestions?: string[];
}

/** Filters live in the URL (shareable, back button works). */
export function useUrlFilters(keys: string[]): [Record<string, string>, (next: Record<string, string>) => void] {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const filters: Record<string, string> = {};
  for (const key of keys) {
    const value = params.get(key);
    if (value) filters[key] = value;
  }
  const set = (next: Record<string, string>) => {
    const search = new URLSearchParams(params.toString());
    for (const key of keys) {
      const value = next[key]?.trim();
      if (value) search.set(key, value);
      else search.delete(key);
    }
    const qs = search.toString();
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  };
  return [filters, set];
}

export function ListFilters({
  fields,
  value,
  onChange,
  storageKey,
  label = "Filters",
}: {
  fields: FilterField[];
  value: Record<string, string>;
  onChange: (next: Record<string, string>) => void;
  /** localStorage key for saved views; omit to disable them. */
  storageKey?: string;
  label?: string;
}) {
  const id = useId();
  const [draft, setDraft] = useState<Record<string, string>>(value);
  const [views, setViews] = useState<SavedView[]>(() => (storageKey ? loadViews(storageKey) : []));
  const [viewName, setViewName] = useState("");
  const [savedMessage, setSavedMessage] = useState("");

  // Keep the inputs in step with the URL (back/forward, a saved view chosen) and reload the
  // saved views when the organization changes: adjusted during render, not in an effect.
  const valueKey = JSON.stringify(value);
  const [seen, setSeen] = useState({ valueKey, storageKey });
  if (seen.valueKey !== valueKey || seen.storageKey !== storageKey) {
    setSeen({ valueKey, storageKey });
    if (seen.valueKey !== valueKey) setDraft(value);
    if (seen.storageKey !== storageKey) setViews(storageKey ? loadViews(storageKey) : []);
  }

  const apply = (event: FormEvent) => {
    event.preventDefault();
    onChange(draft);
  };
  const active = Object.values(value).some(Boolean);

  return (
    <div className="mb-4 rounded-xl border bg-panel p-3 shadow-panel">
      <form onSubmit={apply} aria-label={label} className="flex flex-wrap items-end gap-2">
        {fields.map((field) => (
          <div key={field.key} className="min-w-[10rem] flex-1 sm:max-w-[14rem]">
            <label htmlFor={`${id}-${field.key}`} className="mb-1 block text-xs font-medium text-muted-foreground">
              {field.label}
            </label>
            <Input
              id={`${id}-${field.key}`}
              list={field.suggestions?.length ? `${id}-${field.key}-list` : undefined}
              value={draft[field.key] ?? ""}
              placeholder={field.placeholder ?? "Any"}
              onChange={(e) => setDraft((d) => ({ ...d, [field.key]: e.target.value }))}
              className="h-8"
            />
            {field.suggestions?.length ? (
              <datalist id={`${id}-${field.key}-list`}>
                {field.suggestions.map((s) => (
                  <option key={s} value={s} />
                ))}
              </datalist>
            ) : null}
          </div>
        ))}
        <div className="flex gap-1.5">
          <Button type="submit" size="sm">
            <Filter aria-hidden="true" />
            Apply
          </Button>
          {active ? (
            <Button type="button" size="sm" variant="ghost" onClick={() => onChange({})}>
              <X aria-hidden="true" />
              Clear
            </Button>
          ) : null}
        </div>
      </form>

      {storageKey ? (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t pt-3 text-xs">
          <Bookmark aria-hidden="true" className="size-3.5 text-muted-foreground" />
          <label htmlFor={`${id}-views`} className="text-muted-foreground">
            Saved views
          </label>
          <Select
            id={`${id}-views`}
            className="h-7 w-auto min-w-[9rem] text-xs"
            value=""
            onChange={(e) => {
              const view = views.find((v) => v.name === e.target.value);
              if (view) onChange(view.filters);
            }}
            disabled={views.length === 0}
          >
            <option value="">{views.length ? "Open a view…" : "None saved yet"}</option>
            {views.map((v) => (
              <option key={v.name} value={v.name}>
                {v.name}
              </option>
            ))}
          </Select>
          <form
            className="flex items-center gap-1.5"
            aria-label="Save current filters as a view"
            onSubmit={(e) => {
              e.preventDefault();
              if (!viewName.trim()) return;
              setViews(saveView(storageKey, { name: viewName, filters: value }));
              setSavedMessage(`Saved “${viewName.trim()}” in this browser.`);
              setViewName("");
            }}
          >
            <label htmlFor={`${id}-view-name`} className="sr-only">
              View name
            </label>
            <Input
              id={`${id}-view-name`}
              value={viewName}
              onChange={(e) => setViewName(e.target.value)}
              placeholder="Name these filters"
              className="h-7 w-40 text-xs"
            />
            <Button type="submit" size="xs" variant="outline" disabled={!viewName.trim()}>
              Save view
            </Button>
          </form>
          {views.length ? (
            <ul className="flex flex-wrap gap-1" aria-label="Delete a saved view">
              {views.map((v) => (
                <li key={v.name}>
                  <Button
                    size="xs"
                    variant="ghost"
                    aria-label={`Delete saved view ${v.name}`}
                    onClick={() => setViews(deleteView(storageKey, v.name))}
                  >
                    <Trash2 aria-hidden="true" />
                    {v.name}
                  </Button>
                </li>
              ))}
            </ul>
          ) : null}
          <span role="status" className="text-muted-foreground">
            {savedMessage}
          </span>
        </div>
      ) : null}
    </div>
  );
}
