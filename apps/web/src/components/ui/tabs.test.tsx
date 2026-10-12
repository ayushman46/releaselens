import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

function renderTabs() {
  return render(
    <Tabs defaultValue="a">
      <TabsList aria-label="Setup">
        <TabsTrigger value="a">Collector</TabsTrigger>
        <TabsTrigger value="b">SDK</TabsTrigger>
        <TabsTrigger value="c">Lambda</TabsTrigger>
      </TabsList>
      <TabsContent value="a">Panel A</TabsContent>
      <TabsContent value="b">Panel B</TabsContent>
      <TabsContent value="c">Panel C</TabsContent>
    </Tabs>,
  );
}

describe("Tabs", () => {
  it("wires tabs and panels together and keeps only the selected tab tabbable", () => {
    renderTabs();
    const [a, b] = screen.getAllByRole("tab");
    expect(a).toHaveAttribute("aria-selected", "true");
    expect(a).toHaveAttribute("tabindex", "0");
    expect(b).toHaveAttribute("tabindex", "-1");
    const panel = screen.getByRole("tabpanel", { name: "Collector" });
    expect(panel).toHaveTextContent("Panel A");
    expect(a).toHaveAttribute("aria-controls", panel.id);
  });

  it("moves selection with arrow keys, wrapping, and Home/End", () => {
    renderTabs();
    const tabs = screen.getAllByRole("tab");
    tabs[0]!.focus();
    fireEvent.keyDown(tabs[0]!, { key: "ArrowRight" });
    expect(document.activeElement).toBe(tabs[1]);
    expect(tabs[1]).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Panel B");

    fireEvent.keyDown(tabs[1]!, { key: "End" });
    expect(document.activeElement).toBe(tabs[2]);
    fireEvent.keyDown(tabs[2]!, { key: "ArrowRight" });
    expect(document.activeElement).toBe(tabs[0]);
    fireEvent.keyDown(tabs[0]!, { key: "ArrowLeft" });
    expect(document.activeElement).toBe(tabs[2]);
    fireEvent.keyDown(tabs[2]!, { key: "Home" });
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
  });
});
