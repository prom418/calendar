import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  expect:{ timeout:15_000 },
  retries: 0,
  reporter: "list",
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name:"desktop-chrome", use:{ ...devices["Desktop Chrome"] } },
    { name:"mobile-chrome", use:{ ...devices["Pixel 7"] } },
  ],
});
