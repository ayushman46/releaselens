#!/usr/bin/env node
// Regenerates the API contract without running any server:
//   1. imports the FastAPI app (services/api) with throwaway settings and dumps app.openapi()
//      into openapi/releaselens.json (needs `uv` and the repo's Python env: `uv sync` once);
//   2. runs openapi-typescript to produce src/lib/api/schema.d.ts.
// `pnpm gen:api --skip-python` only re-runs step 2 from the committed JSON (no Python needed).
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const webDir = join(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = join(webDir, "..", "..");
const specPath = join(webDir, "openapi", "releaselens.json");

const PY = [
  "import json",
  "from rl_api.app import create_app",
  "from rl_api.settings import Settings",
  "print(json.dumps(create_app(Settings(env='test', database_url='postgresql+asyncpg://x:y@127.0.0.1:1/z', kafka_bootstrap='127.0.0.1:1', otel_enabled=False)).openapi(), indent=2, sort_keys=True))",
].join("; ");

if (!process.argv.includes("--skip-python")) {
  const out = execFileSync("uv", ["run", "--no-sync", "python", "-c", PY], {
    cwd: repoRoot,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
    stdio: ["ignore", "pipe", "inherit"],
  });
  const spec = JSON.parse(out);
  writeFileSync(specPath, `${JSON.stringify(spec, null, 2)}\n`);
  console.log(`wrote ${specPath} (${Object.keys(spec.paths).length} paths)`);
}

execFileSync(
  join(webDir, "node_modules", ".bin", "openapi-typescript"),
  [specPath, "-o", join(webDir, "src", "lib", "api", "schema.d.ts")],
  { cwd: webDir, stdio: "inherit" },
);
