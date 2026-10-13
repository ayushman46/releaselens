#!/usr/bin/env node
// Copies the static reference data the web app renders into src/data/ (committed):
//   docs/analytics/findings.json        -> src/data/findings.json        (Analytics page)
//   docs/ml/results/*.json              -> src/data/ml/*.json             (Model page)
//   docs/ml/MODEL_CARD.md (excerpts)    -> src/data/ml/model-card.json    (quoted verbatim)
// The Docker build context is apps/web only, so the copies are committed; re-run this after a
// new analytics report or training run. `--check` exits 1 when a copy is stale (used by CI).
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const webDir = join(dirname(fileURLToPath(import.meta.url)), "..");
const docs = join(webDir, "..", "..", "docs");
const out = join(webDir, "src", "data");
const check = process.argv.includes("--check");

if (!existsSync(docs)) {
  console.log(`no ${docs} (building outside the repo?): keeping the committed copies`);
  process.exit(0);
}

const outputs = new Map();
outputs.set(join(out, "findings.json"), readFileSync(join(docs, "analytics", "findings.json"), "utf8"));
for (const file of readdirSync(join(docs, "ml", "results")).filter((f) => f.endsWith(".json")).sort()) {
  outputs.set(join(out, "ml", file), readFileSync(join(docs, "ml", "results", file), "utf8"));
}

// Model-card sentences the Model page quotes word for word.
const card = readFileSync(join(docs, "ml", "MODEL_CARD.md"), "utf8");
const bullet = (title) => {
  const line = card.split("\n").find((l) => l.startsWith(`- **${title}`));
  return line ? line.replace(/^- \*\*[^*]+\*\*\s*/, "").trim() : null;
};
const excerpts = {
  source: "docs/ml/MODEL_CARD.md",
  version: (card.match(/\*\*Version:\*\* `([^`]+)`/) ?? [])[1] ?? null,
  persistence: bullet("Persistence vs new breakage."),
  ci_failure_not_outage: bullet("CI failure ≠ outage."),
  travis_transfer: bullet("Travis-era transfer risk."),
  explanations: bullet("Explanations"),
  intended_use: card
    .split("## Intended use")[1]
    ?.split("\n## ")[0]
    .split("\n")
    .filter((l) => l.startsWith("- "))
    .map((l) => l.slice(2).replace(/\*\*/g, "").trim()) ?? [],
};
outputs.set(join(out, "ml", "model-card.json"), `${JSON.stringify(excerpts, null, 2)}\n`);

let stale = 0;
for (const [path, content] of outputs) {
  const current = existsSync(path) ? readFileSync(path, "utf8") : null;
  if (current === content) continue;
  stale += 1;
  if (check) {
    console.error(`stale: ${path}`);
  } else {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, content);
    console.log(`wrote ${path}`);
  }
}
if (check && stale) process.exit(1);
console.log(stale ? `${check ? "stale" : "updated"}: ${stale} file(s)` : "src/data is up to date");
