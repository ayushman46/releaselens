"use client";

import { CornerDownLeft, Moon, Search, Sun, type LucideIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";

import { Dialog } from "@/components/ui/dialog";
import { Kbd, ModKey } from "@/components/ui/kbd";
import { APP_NAV, DOCS_NAV, SITE_NAV } from "@/lib/navigation";
import { applyTheme } from "@/lib/theme";
import { cn } from "@/lib/utils";

export interface CommandItem {
  id: string;
  label: string;
  group: string;
  icon: LucideIcon;
  description?: string;
  keywords?: string[];
  href?: string;
  run?: () => void;
}

function currentTheme(): "dark" | "light" {
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}

export function buildCommandItems(): CommandItem[] {
  const items: CommandItem[] = [];
  for (const group of APP_NAV) {
    for (const item of group.items) {
      items.push({ id: `app:${item.href}`, group: "Dashboard", ...item });
    }
  }
  for (const item of SITE_NAV) items.push({ id: `site:${item.href}`, group: "Website", ...item });
  for (const item of DOCS_NAV) items.push({ id: `docs:${item.href}`, group: "Docs", ...item });
  items.push({
    id: "action:theme",
    group: "Actions",
    label: "Switch theme",
    icon: Sun,
    description: "Toggle between the dark and light theme",
    keywords: ["dark", "light", "appearance", "mode"],
    run: () => applyTheme(currentTheme() === "dark" ? "light" : "dark"),
  });
  return items;
}

/** Every whitespace-separated term must appear in the label, group, description or keywords. */
export function filterCommandItems(items: CommandItem[], query: string): CommandItem[] {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return items;
  return items.filter((item) => {
    const haystack = [item.label, item.group, item.description ?? "", ...(item.keywords ?? [])]
      .join(" ")
      .toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}

interface CommandMenuContextValue {
  open: () => void;
}

const CommandMenuContext = createContext<CommandMenuContextValue | null>(null);

export function useCommandMenu(): CommandMenuContextValue {
  const ctx = useContext(CommandMenuContext);
  if (!ctx) throw new Error("useCommandMenu must be used inside <CommandMenuProvider>");
  return ctx;
}

/** Mounts the command menu once and binds ⌘K / Ctrl+K globally. */
export function CommandMenuProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((value) => !value);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const value = useMemo(() => ({ open: () => setOpen(true) }), []);
  const close = useCallback(() => setOpen(false), []);

  return (
    <CommandMenuContext.Provider value={value}>
      {children}
      <Dialog
        open={open}
        onClose={close}
        label="Command menu"
        description="Type to filter pages and actions. Use the arrow keys to choose and Enter to open."
        placement="top"
        showClose={false}
        initialFocus={inputRef}
        className="max-w-xl overflow-hidden p-0"
      >
        <CommandPalette inputRef={inputRef} onClose={close} />
      </Dialog>
    </CommandMenuContext.Provider>
  );
}

function CommandPalette({
  inputRef,
  onClose,
}: {
  inputRef: RefObject<HTMLInputElement | null>;
  onClose: () => void;
}) {
  const router = useRouter();
  const listId = useId();
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const allItems = useMemo(() => buildCommandItems(), []);
  const results = useMemo(() => filterCommandItems(allItems, query), [allItems, query]);
  const activeItem = results[Math.min(active, results.length - 1)];
  const optionId = (item: CommandItem) => `${listId}-${item.id.replace(/[^a-zA-Z0-9-]/g, "_")}`;

  useEffect(() => {
    if (!activeItem) return;
    document.getElementById(optionId(activeItem))?.scrollIntoView?.({ block: "nearest" });
    // optionId is derived from listId, which is stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeItem]);

  const choose = (item: CommandItem) => {
    onClose();
    if (item.href) router.push(item.href);
    else item.run?.();
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (results.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((i) => (Math.min(i, results.length - 1) + 1) % results.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((i) => (Math.min(i, results.length - 1) - 1 + results.length) % results.length);
    } else if (event.key === "Enter" && activeItem) {
      event.preventDefault();
      choose(activeItem);
    }
  };

  const groups = results.reduce<Array<{ name: string; items: CommandItem[] }>>((acc, item) => {
    const group = acc.find((g) => g.name === item.group);
    if (group) group.items.push(item);
    else acc.push({ name: item.group, items: [item] });
    return acc;
  }, []);

  return (
    <div>
      <div className="flex items-center gap-2.5 border-b px-4 focus-within:border-ring/60">
        <Search aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
        <input
          ref={inputRef}
          type="text"
          role="combobox"
          aria-expanded="true"
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={activeItem ? optionId(activeItem) : undefined}
          aria-label="Search pages and actions"
          placeholder="Search pages and actions…"
          autoComplete="off"
          spellCheck={false}
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          className="h-12 min-w-0 flex-1 bg-transparent text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none"
        />
        <Kbd aria-hidden="true">Esc</Kbd>
      </div>

      <div id={listId} role="listbox" aria-label="Pages and actions" className="max-h-[min(24rem,60vh)] overflow-y-auto p-2">
        {groups.map((group) => {
          const headingId = `${listId}-group-${group.name}`;
          return (
            <div key={group.name} role="group" aria-labelledby={headingId} className="mb-1 last:mb-0">
              <div id={headingId} className="px-2 pt-2 pb-1 text-[11px] font-medium tracking-wide text-muted-foreground uppercase">
                {group.name}
              </div>
              {group.items.map((item) => {
                const selected = item === activeItem;
                const Icon = item.id === "action:theme" ? ThemeIcon : item.icon;
                return (
                  <div
                    key={item.id}
                    id={optionId(item)}
                    role="option"
                    aria-selected={selected}
                    onMouseMove={() => setActive(results.indexOf(item))}
                    onClick={() => choose(item)}
                    className={cn(
                      "flex cursor-pointer items-center gap-3 rounded-md px-2 py-2 text-sm",
                      selected ? "bg-subtle text-foreground" : "text-foreground-secondary",
                    )}
                  >
                    <Icon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
                    <span className="truncate font-medium">{item.label}</span>
                    {item.description ? (
                      <span className="hidden truncate text-xs text-muted-foreground sm:inline">{item.description}</span>
                    ) : null}
                    {selected ? (
                      <CornerDownLeft aria-hidden="true" className="ml-auto size-3.5 shrink-0 text-muted-foreground" />
                    ) : null}
                  </div>
                );
              })}
            </div>
          );
        })}
        {results.length === 0 ? (
          <p className="px-2 py-8 text-center text-sm text-muted-foreground">
            No pages or actions match “{query}”.
          </p>
        ) : null}
      </div>

      <p aria-live="polite" className="sr-only">
        {results.length === 1 ? "1 result" : `${results.length} results`}
      </p>

      <div className="flex items-center gap-4 border-t px-4 py-2 text-[11px] text-muted-foreground">
        <span className="inline-flex items-center gap-1">
          <Kbd aria-hidden="true">↑</Kbd>
          <Kbd aria-hidden="true">↓</Kbd> to choose
        </span>
        <span className="inline-flex items-center gap-1">
          <Kbd aria-hidden="true">↵</Kbd> to open
        </span>
        <span className="ml-auto hidden items-center gap-1 sm:inline-flex">
          <ModKey letter="K" /> to toggle
        </span>
      </div>
    </div>
  );
}

function ThemeIcon(props: { className?: string; "aria-hidden"?: "true" }) {
  return (
    <>
      <Sun {...props} className={cn(props.className, "hidden dark:block")} />
      <Moon {...props} className={cn(props.className, "dark:hidden")} />
    </>
  );
}

/** The top-bar search button that opens the command menu. */
export function CommandMenuTrigger({ className }: { className?: string }) {
  const { open } = useCommandMenu();
  return (
    <button
      type="button"
      onClick={open}
      aria-haspopup="dialog"
      className={cn(
        "inline-flex h-8 shrink-0 items-center gap-2 rounded-md border bg-panel px-2.5 text-sm text-muted-foreground shadow-panel transition-colors hover:border-border-strong/60 hover:text-foreground sm:pr-1.5",
        className,
      )}
    >
      <Search aria-hidden="true" className="size-3.5" />
      <span className="hidden lg:inline">Search or jump to…</span>
      {/* Icon-only on phones; the word stays available to assistive tech. */}
      <span className="sr-only sm:not-sr-only lg:hidden">Search</span>
      <ModKey letter="K" className="ml-2 hidden sm:inline-flex" />
    </button>
  );
}
