// Two end-to-end journeys against the real API (eval/demo_api.py: real routes, auth, encryption and audit, with a
// SIMULATED model, the scripted oracle) and the production build of the UI (npm run build first).
import { randomBytes } from "node:crypto";

import { defineConfig, devices } from "@playwright/test";

const API_PORT = Number(process.env.E2E_API_PORT ?? 8765);
const UI_PORT = Number(process.env.E2E_UI_PORT ?? 3100);
// The demo users' password, made up for this run and shared with the tests through the environment.
process.env.E2E_DEMO_PASSWORD ??= randomBytes(12).toString("base64url");

export default defineConfig({
  testDir: "tests/e2e",
  timeout: 30_000,
  workers: 1, // both journeys review cases of the same demo database
  reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${UI_PORT}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `cd .. && uv run python -m eval.demo_api --port ${API_PORT} --per-trap 2 --feedback ""`,
      url: `http://127.0.0.1:${API_PORT}/health`,
      env: { PRIORAUTH_DEMO_PASSWORD: process.env.E2E_DEMO_PASSWORD },
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      command: `npx next start --port ${UI_PORT}`,
      url: `http://127.0.0.1:${UI_PORT}/login`,
      env: { PRIORAUTH_API_URL: `http://127.0.0.1:${API_PORT}` },
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
