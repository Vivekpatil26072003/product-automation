import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

// E2E against the real stack: infra/local (Postgres, Redis, object store, ClamAV) must be running and
// the dev database migrated and seeded (see README). The API and web app are started here; the worker
// is started by global-setup because it has no port to wait for.

const root = path.resolve(__dirname, "../..");
const python = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
const pyEnv = { PYTHONPATH: ["services/api", "services"].join(path.delimiter) };

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  use: { baseURL: "http://localhost:3000", trace: "retain-on-failure" },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
  webServer: [
    {
      command: `"${python}" -m uvicorn app.main:app --port 8000`,
      cwd: root,
      env: pyEnv,
      url: "http://127.0.0.1:8000/api/v1/health/live",
      reuseExistingServer: true,
      timeout: 60_000,
    },
    {
      command: "npm run dev",
      url: "http://localhost:3000/login",
      env: { NEXT_PUBLIC_DEV_AUTH: "true", BACKEND_URL: "http://127.0.0.1:8000" },
      reuseExistingServer: true,
      timeout: 120_000,
    },
  ],
});
