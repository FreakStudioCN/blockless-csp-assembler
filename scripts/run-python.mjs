import { spawnSync } from "node:child_process";

const command = process.platform === "win32" ? "python" : "python3";
const result = spawnSync(command, process.argv.slice(2), { stdio: "inherit" });
process.exit(result.status ?? 1);
