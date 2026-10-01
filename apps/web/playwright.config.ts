import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end: real browser → Next.js BFF (standalone build) → real API with the worker
 * embedded → SQLite. Requires `npm run build` first (CI does this) and a Python env with the
 * API installed (`SF_PYTHON`, default `python`).
 */
const API_PORT = 8199;
const WEB_PORT = 3199;
const python = process.env.SF_PYTHON ?? "python";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `${python} scripts/e2e_server.py ${API_PORT}`,
      cwd: "../api",
      url: `http://127.0.0.1:${API_PORT}/readyz`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: "node .next/standalone/server.js",
      url: `http://127.0.0.1:${WEB_PORT}/login`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: {
        PORT: String(WEB_PORT),
        HOSTNAME: "127.0.0.1",
        SF_API_URL: `http://127.0.0.1:${API_PORT}`,
        SF_COOKIE_SECURE: "false", // plain http in tests only
      },
    },
  ],
});
