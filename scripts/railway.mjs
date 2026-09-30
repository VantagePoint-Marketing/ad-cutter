// Runs the repo-local Railway CLI with its binary folder on the PATH.
// Needed on Windows: the IaC SDK checks the CLI version by running `railway --version`, and only the .cmd shim
// is on the PATH under npm/npx, which Node refuses to execute directly. Usage: npm run railway -- config plan
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";

const require = createRequire(import.meta.url);
const bin = path.join(path.dirname(require.resolve("@railway/cli/package.json")), "bin");
const exe = path.join(bin, process.platform === "win32" ? "railway.exe" : "railway");
process.env.PATH = `${bin}${path.delimiter}${process.env.PATH ?? ""}`;
process.env._ = exe; // the SDK's check runs `$_ --version` when `_` is set; make sure it is this executable
const result = spawnSync(exe, process.argv.slice(2), { stdio: "inherit" });
if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}
process.exit(result.status ?? 1);
