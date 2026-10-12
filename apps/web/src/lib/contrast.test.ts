// @vitest-environment node
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { contrastRatio, parseHex, readTokenBlock } from "@/lib/contrast";

// Vitest runs from apps/web (see vitest.config.mts).
const css = readFileSync(resolve(process.cwd(), "src/app/globals.css"), "utf8");

const THEMES = {
  light: readTokenBlock(css, ":root"),
  dark: readTokenBlock(css, ".dark"),
} as const;

const TEXT_MIN = 4.5;
const UI_MIN = 3;

const SURFACES = ["bg", "panel", "raised", "subtle"] as const;
const STATUS = ["healthy", "warning", "high", "critical", "unknown", "info"] as const;

type Pair = { fg: string; bg: string; min: number };

/** Every foreground/background combination the components actually render. */
function pairsFor(): Pair[] {
  const pairs: Pair[] = [];
  const add = (fg: string, bg: string, min: number) => pairs.push({ fg, bg, min });

  // Text on every surface.
  for (const text of ["text", "text-secondary", "text-muted", "brand-text"]) {
    for (const surface of SURFACES) add(text, surface, TEXT_MIN);
  }
  // Text on filled buttons.
  add("primary-fg", "primary", TEXT_MIN);
  add("brand-fg", "brand", TEXT_MIN);

  // UI components (WCAG 1.4.11): focus ring, control boundaries, brand indicators.
  for (const ui of ["ring", "border-strong", "brand"]) {
    for (const surface of SURFACES) add(ui, surface, UI_MIN);
  }

  // Status families: the -fg colour is used as text (pills, callout titles) and as icon colour.
  for (const status of STATUS) {
    add(`${status}-fg`, `${status}-bg`, TEXT_MIN);
    for (const surface of SURFACES) add(`${status}-fg`, surface, TEXT_MIN);
    // Callout bodies render regular text on the tinted fill.
    add("text", `${status}-bg`, TEXT_MIN);
    add("text-secondary", `${status}-bg`, TEXT_MIN);
  }
  return pairs;
}

const PAIRS = pairsFor();

describe.each(Object.entries(THEMES))("%s theme tokens", (theme, tokens) => {
  const value = (name: string): string => {
    const v = tokens[name];
    if (!v) throw new Error(`--${name} is not defined in the ${theme} theme`);
    return v;
  };

  it("defines every token the pairs use as an opaque hex colour", () => {
    const names = new Set(PAIRS.flatMap((p) => [p.fg, p.bg]));
    for (const name of names) expect(() => parseHex(value(name)), `--${name}`).not.toThrow();
  });

  it.each(PAIRS.map((p) => [p.fg, p.bg, p.min] as const))(
    "--%s on --%s meets %s:1",
    (fg, bg, min) => {
      const ratio = contrastRatio(value(fg), value(bg));
      expect(ratio, `${theme}: --${fg} (${value(fg)}) on --${bg} (${value(bg)})`).toBeGreaterThanOrEqual(
        min,
      );
    },
  );

  it("keeps decorative separators visible without competing with content", () => {
    // --border is decorative (WCAG 1.4.11 exempt); it must still read as a line, but stay calm.
    for (const surface of ["bg", "panel"]) {
      const ratio = contrastRatio(value("border"), value(surface));
      expect(ratio).toBeGreaterThan(1.15);
      expect(ratio).toBeLessThan(UI_MIN);
    }
  });

  it("reports the lowest asserted ratio", () => {
    const ratios = PAIRS.map((p) => ({
      pair: `--${p.fg} on --${p.bg}`,
      ratio: contrastRatio(value(p.fg), value(p.bg)),
      min: p.min,
    }));
    const lowestText = ratios.filter((r) => r.min === TEXT_MIN).sort((a, b) => a.ratio - b.ratio)[0]!;
    const lowestUi = ratios.filter((r) => r.min === UI_MIN).sort((a, b) => a.ratio - b.ratio)[0]!;
    if (process.env.RL_PRINT_CONTRAST) {
      console.info(
        `[contrast] ${theme}: lowest text ${lowestText.ratio.toFixed(2)}:1 (${lowestText.pair}); ` +
          `lowest UI ${lowestUi.ratio.toFixed(2)}:1 (${lowestUi.pair})`,
      );
    }
    expect(lowestText.ratio).toBeGreaterThanOrEqual(TEXT_MIN);
    expect(lowestUi.ratio).toBeGreaterThanOrEqual(UI_MIN);
  });
});

describe("contrast maths", () => {
  it("matches the WCAG reference values", () => {
    expect(contrastRatio("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrastRatio("#ffffff", "#ffffff")).toBeCloseTo(1, 5);
    // #767676 on white is the classic 4.54:1 grey.
    expect(contrastRatio("#767676", "#ffffff")).toBeCloseTo(4.54, 2);
  });

  it("rejects colours it cannot measure", () => {
    expect(() => parseHex("rgb(0 0 0 / 0.5)")).toThrow();
  });
});
