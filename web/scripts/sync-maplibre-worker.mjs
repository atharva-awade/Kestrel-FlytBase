/**
 * Copy MapLibre's worker bundle into `public/` so it can be served over HTTP.
 *
 * MapLibre v6 bootstraps its worker from `import.meta.url`:
 *
 *     function defaultWorkerUrl() {
 *       const moduleUrl = import.meta.url;
 *       if (!/^https?:/.test(moduleUrl)) return "";     // <- bail-out
 *       ...
 *     }
 *
 * Webpack inlines `import.meta.url` as a `file:` URL at build time, so that test
 * fails and the browser ends up running `new Worker("", { type: "module" })`.
 * An empty URL resolves against the document, so the worker tries to load the
 * HTML page as a module script and dies.
 *
 * The failure is close to invisible: the style JSON, tiles.json and sprite are
 * all fetched on the main thread and return 200, and the controls and
 * attribution are plain DOM, so the map looks alive. Only tile *decoding* runs
 * in the worker, so the canvas silently never paints, and no `error` event fires
 * because nothing errored, something just never answered.
 *
 * **The worker is not one file.** `maplibre-gl-worker.mjs` opens with
 * `import {...} from "./maplibre-gl-shared.mjs"`. Copying the worker alone
 * reproduces the original bug exactly: the worker loads, requests its sibling,
 * gets Next's 404 HTML page with `Content-Type: text/html`, and the module
 * worker dies. So every file the worker reaches for has to come with it, and
 * this script verifies that rather than assuming it.
 */
import { copyFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const webRoot = join(here, "..");
const distDir = join(webRoot, "node_modules", "maplibre-gl", "dist");
const outDir = join(webRoot, "public", "maplibre");

const pkgPath = join(webRoot, "node_modules", "maplibre-gl", "package.json");
if (!existsSync(pkgPath)) {
  console.warn("[maplibre] not installed; skipping worker sync");
  process.exit(0);
}
const version = JSON.parse(readFileSync(pkgPath, "utf8")).version;

/** Follow relative `import ... from "./x.mjs"` specifiers so nothing is missed. */
function collect(entry, seen = new Set()) {
  if (seen.has(entry)) return seen;
  const full = join(distDir, entry);
  if (!existsSync(full)) {
    throw new Error(`[maplibre] ${entry} is missing from dist`);
  }
  seen.add(entry);
  const src = readFileSync(full, "utf8");
  for (const m of src.matchAll(/from\s*["'](\.\/[^"']+\.mjs)["']/g)) {
    collect(m[1].replace(/^\.\//, ""), seen);
  }
  return seen;
}

let files;
try {
  files = collect("maplibre-gl-worker.mjs");
} catch (e) {
  console.warn(String(e.message));
  process.exit(0);
}

mkdirSync(outDir, { recursive: true });
for (const f of files) copyFileSync(join(distDir, f), join(outDir, f));

// Prove the copy is closed over its own imports, rather than trusting the walk.
const missing = [];
for (const f of files) {
  const src = readFileSync(join(outDir, f), "utf8");
  for (const m of src.matchAll(/from\s*["'](\.\/[^"']+\.mjs)["']/g)) {
    const dep = m[1].replace(/^\.\//, "");
    if (!existsSync(join(outDir, dep))) missing.push(`${f} -> ${dep}`);
  }
}
if (missing.length) {
  console.error(`[maplibre] unresolved worker imports: ${missing.join(", ")}`);
  process.exit(1);
}

console.log(
  `[maplibre] worker ${version} -> public/maplibre/ (${[...files].join(", ")})`,
);
