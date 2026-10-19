// End-to-end journeys against the demo API (python -m priorauth.demo: real routes, auth, encryption and audit; the
// model's responses are real ones recorded by eval/pipeline.py and replayed) and the production build of the UI
// (npm run build first).
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
      command: `cd .. && uv run python -m priorauth.demo --port ${API_PORT}`,
      url: `http://127.0.0.1:${API_PORT}/healthz`,
      env: { PRIORAUTH_DEMO_REVIEWER_PASSWORD: process.env.E2E_DEMO_PASSWORD },
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
