import { defineConfig } from '@playwright/test'
import path from 'node:path'

export default defineConfig({
  testDir: './e2e', fullyParallel: false,
  outputDir: process.env.PURRAI_TEST_OUTPUT || path.join('test-results', `run-${Date.now()}`),
  use: { baseURL: process.env.PURRAI_TEST_BASE_URL || 'http://127.0.0.1:5174', viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.PURRAI_BROWSER_EXECUTABLE ? { executablePath: process.env.PURRAI_BROWSER_EXECUTABLE } : {} },
  projects: [
    { name: 'mocked', testIgnore: 'integration.spec.ts' },
    { name: 'integration', testMatch: 'integration.spec.ts' },
  ],
  webServer: process.env.PURRAI_TEST_BASE_URL ? undefined : [
    { command: `"${process.env.PURRAI_TEST_PYTHON || 'python'}" ../scripts/browser_test.py`,
      url: 'http://127.0.0.1:8877/__local/status', reuseExistingServer: false, timeout: 120000 },
    { command: 'npm run dev -- --port 5174', url: 'http://127.0.0.1:5174', reuseExistingServer: false,
      env: { PURRAI_DEV_API_URL: 'http://127.0.0.1:8877' } },
  ],
  reporter: [['list']],
})
