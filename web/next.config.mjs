import fs from "node:fs";
import path from "node:path";

/**
 * The repository keeps a single `.env` at its root, which is what `.env.example`
 * and the README tell you to fill in. Next only reads env files from its own
 * directory, so `NEXT_PUBLIC_MAPTILER_KEY` never reached the browser and the site
 * map silently fell back to "no key set", with the key sitting right there in
 * the file the docs told you to put it in.
 *
 * Only `NEXT_PUBLIC_`-prefixed names are read. That prefix is the whole security
 * boundary here: the same file holds the NVIDIA and Groq keys, and anything
 * lifted into this object is compiled into client JavaScript. Widening this
 * filter would publish them.
 */
function publicEnvFromRepoRoot() {
  const out = {};
  const file = path.join(process.cwd(), "..", ".env");
  if (!fs.existsSync(file)) return out;

  for (const line of fs.readFileSync(file, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq < 0) continue;

    const key = trimmed.slice(0, eq).trim();
    if (!key.startsWith("NEXT_PUBLIC_")) continue;

    // A real environment variable always wins over the file.
    out[key] =
      process.env[key] ?? trimmed.slice(eq + 1).trim().replace(/^["']|["']$/g, "");
  }
  return out;
}

const publicEnv = publicEnvFromRepoRoot();

/** @type {import('next').NextConfig} */
const API =
  process.env.NEXT_PUBLIC_API_URL || publicEnv.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

const nextConfig = {
  env: publicEnv,

  /* A production build and a dev server share `.next` by default, so running
   * `next build` while `next dev` is up overwrites the chunks the browser is
   * actively loading, which surfaces as `__webpack_modules__[moduleId] is not a
   * function`, or a 404 on the stylesheet. `npm run build:check` sets
   * NEXT_DIST_DIR so verification builds land elsewhere and cannot disturb a
   * running dev server. Plain `npm run build` still writes `.next` for deploy. */
  distDir: process.env.NEXT_DIST_DIR || ".next",

  /* The dev proxy truncates request bodies at 10 MB and then drops the socket,
   * which surfaces to the user as a bare 500. Video uploads go straight to the
   * API origin to avoid it entirely; this raises the ceiling as well, so a
   * deployment that serves both from one hostname still works. */
  experimental: {
    proxyTimeout: 15 * 60 * 1000,
    middlewareClientMaxBodySize: 220 * 1024 * 1024,
  },

  reactStrictMode: true,
  // three.js and react-globe.gl ship ESM that Next needs to transpile.
  transpilePackages: ["react-globe.gl", "three", "three-globe"],
  eslint: { ignoreDuringBuilds: true },
  typescript: { ignoreBuildErrors: false },
  async rewrites() {
    // Proxy the API in development so the browser makes same-origin requests and
    // no provider key is ever exposed to it; all model calls stay server-side.
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};

export default nextConfig;
