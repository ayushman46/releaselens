import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CodeBlock } from "@/components/ui/code-block";

afterEach(() => {
  vi.unstubAllGlobals();
});

async function clickCopy() {
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "Copy deploy.sh" }));
  });
}

describe("CodeBlock", () => {
  it("copies the code with the Clipboard API and announces it", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText } });
    render(<CodeBlock code="echo hi" title="deploy.sh" />);

    await clickCopy();
    expect(writeText).toHaveBeenCalledWith("echo hi");
    expect(screen.getByText("Copied")).toBeVisible();
    expect(screen.getByText("Copied to clipboard")).toBeInTheDocument();
  });

  it("falls back to selecting the code when the clipboard is unavailable", async () => {
    vi.stubGlobal("navigator", { ...navigator, clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    render(<CodeBlock code="echo hi" title="deploy.sh" />);

    await clickCopy();
    expect(screen.getByText("Selected, copy manually")).toBeVisible();
    expect(window.getSelection()?.toString()).toBe("echo hi");
    expect(screen.getByText(/The code is selected/)).toBeInTheDocument();
  });
});
