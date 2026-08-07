/**
 * A production build that cannot disturb a running dev server.
 *
 * `next build` and `next dev` both write to `.next`. Running a build to verify a
 * change while the dev server is up overwrites the chunks the browser is actively
 * loading, and the page dies with `__webpack_modules__[moduleId] is not a
 * function` — an error that looks like a code fault but is purely a collision
 * between two processes sharing one output directory.
 *
 * This points the build at `.next-check` instead. Setting the variable inline
 * (`NEXT_DIST_DIR=… next build`) is not portable to PowerShell or cmd, hence the
 * wrapper rather than a one-line npm script.
 */
import { spawn } from "node:child_process";

const child = spawn("next", ["build"], {
  stdio: "inherit",
  shell: true,
  env: { ...process.env, NEXT_DIST_DIR: ".next-check" },
});

child.on("exit", (code) => process.exit(code ?? 1));
