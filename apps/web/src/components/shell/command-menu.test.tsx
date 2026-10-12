import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildCommandItems,
  CommandMenuProvider,
  CommandMenuTrigger,
  filterCommandItems,
} from "@/components/shell/command-menu";

const nav = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: nav.push, prefetch: vi.fn(), replace: vi.fn(), back: vi.fn() }),
  usePathname: () => "/app",
}));

function renderMenu() {
  return render(
    <CommandMenuProvider>
      <button type="button">Somewhere else</button>
      <CommandMenuTrigger />
    </CommandMenuProvider>,
  );
}

function pressModK(init: KeyboardEventInit = { ctrlKey: true }) {
  act(() => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", bubbles: true, ...init }));
  });
}

beforeEach(() => {
  nav.push.mockReset();
});

afterEach(() => {
  document.body.style.overflow = "";
});

describe("command menu", () => {
  it("opens with Ctrl+K, focuses the search box, and closes with Escape restoring focus", () => {
    renderMenu();
    const before = screen.getByRole("button", { name: "Somewhere else" });
    before.focus();
    expect(screen.queryByRole("dialog")).toBeNull();

    pressModK({ ctrlKey: true });
    const dialog = screen.getByRole("dialog", { name: "Command menu" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    const input = within(dialog).getByRole("combobox", { name: "Search pages and actions" });
    expect(document.activeElement).toBe(input);

    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(before);
  });

  it("toggles with ⌘K on macOS", () => {
    renderMenu();
    pressModK({ metaKey: true });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    pressModK({ metaKey: true });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("ignores plain K", () => {
    renderMenu();
    pressModK({});
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("filters as you type and navigates with the arrow keys and Enter", () => {
    renderMenu();
    pressModK();
    const input = screen.getByRole("combobox");
    fireEvent.change(input, { target: { value: "deploy" } });

    const options = screen.getAllByRole("option");
    expect(options.length).toBeGreaterThan(0);
    expect(options[0]).toHaveAttribute("aria-selected", "true");
    expect(input).toHaveAttribute("aria-activedescendant", options[0]!.id);
    expect(options[0]).toHaveTextContent("Deployments");

    fireEvent.keyDown(input, { key: "ArrowDown" });
    const second = screen.getAllByRole("option")[1]!;
    expect(second).toHaveAttribute("aria-selected", "true");
    expect(input).toHaveAttribute("aria-activedescendant", second.id);

    fireEvent.keyDown(input, { key: "ArrowUp" });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(nav.push).toHaveBeenCalledWith("/app/deployments");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("opens from the top-bar trigger and traps Tab inside the dialog", () => {
    renderMenu();
    fireEvent.click(screen.getByRole("button", { name: /search/i }));
    const dialog = screen.getByRole("dialog");
    const input = within(dialog).getByRole("combobox");
    expect(document.activeElement).toBe(input);
    // The search box is the only tabbable element: Tab and Shift+Tab stay on it.
    fireEvent.keyDown(input, { key: "Tab" });
    expect(dialog.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(input, { key: "Tab", shiftKey: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("says when nothing matches", () => {
    renderMenu();
    pressModK();
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "zzzz-nothing" } });
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText(/No pages or actions match/)).toBeInTheDocument();
    expect(screen.getByText("0 results")).toBeInTheDocument();
  });
});

describe("command items", () => {
  it("lists every dashboard page and requires all search terms to match", () => {
    const items = buildCommandItems();
    const hrefs = items.map((i) => i.href);
    for (const href of [
      "/app",
      "/app/deployments",
      "/app/regressions",
      "/app/risk",
      "/app/services",
      "/app/service-map",
      "/app/analytics",
      "/app/model",
      "/app/settings",
      "/pricing",
      "/docs",
    ]) {
      expect(hrefs).toContain(href);
    }
    expect(filterCommandItems(items, "service map").map((i) => i.label)).toContain("Service map");
    expect(filterCommandItems(items, "")).toHaveLength(items.length);
  });
});
