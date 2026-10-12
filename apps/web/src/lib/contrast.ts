/**
 * WCAG 2.x contrast maths for the design-token test (src/lib/contrast.test.ts).
 * Only opaque hex colours are supported: tokens that carry text or UI meaning must be opaque so
 * their contrast is the same on every surface.
 */

export type Rgb = readonly [number, number, number];

export function parseHex(value: string): Rgb {
  const hex = value.trim().replace(/^#/, "");
  const full =
    hex.length === 3
      ? hex
          .split("")
          .map((c) => c + c)
          .join("")
      : hex;
  if (!/^[0-9a-f]{6}$/i.test(full)) {
    throw new Error(`Not an opaque hex colour: ${value}`);
  }
  return [0, 2, 4].map((i) => Number.parseInt(full.slice(i, i + 2), 16)) as unknown as Rgb;
}

function channel(c8: number): number {
  const c = c8 / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

/** Relative luminance (WCAG 2.x definition). */
export function luminance([r, g, b]: Rgb): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

export function contrastRatio(a: string, b: string): number {
  const la = luminance(parseHex(a));
  const lb = luminance(parseHex(b));
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * Reads the custom properties declared directly inside the first `selector { … }` block of a
 * stylesheet. `var(--x)` references are resolved against the same block.
 */
export function readTokenBlock(css: string, selector: string): Record<string, string> {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|\\n)${escaped}\\s*\\{([^}]*)\\}`).exec(css);
  if (!match?.[1]) throw new Error(`No "${selector} { … }" block found`);
  const tokens: Record<string, string> = {};
  for (const [, name, value] of match[1].matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) {
    if (name && value) tokens[name] = value.trim();
  }
  for (const [name, value] of Object.entries(tokens)) {
    const ref = /^var\(--([\w-]+)\)$/.exec(value);
    const resolved = ref?.[1] ? tokens[ref[1]] : undefined;
    if (resolved) tokens[name] = resolved;
  }
  return tokens;
}
