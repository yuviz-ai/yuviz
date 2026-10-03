import { defineConfig } from "@playwright/test";

// The spec runs against a built admin-ui (`next build`, then `next start`) and
// a real Config service. NEXT_PUBLIC_* values are baked in at build time, so
// build with NEXT_PUBLIC_CONFIG_SERVICE_URL pointing at the Config service the
// spec seeds (E2E_CONFIG_URL, default http://localhost:8010) and
// NEXT_PUBLIC_WEBCALL_URL at any ws:// address (voice tests fake the socket).
// Config must allow this origin for CORS and have Redis (test credentials).
const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:3010";

export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL,
    permissions: ["microphone"],
    launchOptions: { args: ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"] },
  },
  webServer: {
    command: `npx next start -p ${new URL(baseURL).port}`,
    url: baseURL,
    reuseExistingServer: true,
  },
});
