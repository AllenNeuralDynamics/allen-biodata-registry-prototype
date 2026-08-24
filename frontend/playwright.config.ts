// Playwright config — runs the agent approve/reject E2E.
// To execute locally:
//   npm install --save-dev @playwright/test
//   npx playwright install chromium
//   BIODATA_REGISTRY_API_BASE=...  BIODATA_REGISTRY_JWT=...  npx playwright test
//
// In CI, set the same env vars; the QC4 pipeline launches a fresh
// frontend dev server and runs the spec against it.
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 1,
  workers: 1, // serial — agent state is shared across the test
  use: {
    baseURL: process.env.PLAYWRIGHT_BASE_URL || "http://localhost:5173",
    headless: true,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: process.env.PLAYWRIGHT_NO_WEBSERVER
    ? undefined
    : {
        command: "npm run dev",
        url: "http://localhost:5173",
        reuseExistingServer: true,
        timeout: 60_000,
      },
});
