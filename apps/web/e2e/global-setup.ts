import { spawn } from "node:child_process";
import path from "node:path";

// Starts one worker (python -m workers) for the whole run and returns the teardown that stops it.
export default async function globalSetup() {
  const root = path.resolve(__dirname, "../../..");
  const python = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const worker = spawn(python, ["-m", "workers"], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: ["services/api", "services"].join(path.delimiter) },
    stdio: "ignore",
  });
  await new Promise((resolve) => setTimeout(resolve, 3000));
  if (worker.exitCode !== null) throw new Error(`worker exited early with code ${worker.exitCode}`);
  return async () => {
    worker.kill();
  };
}
