import { defineConfig } from '@playwright/test'
import path from 'node:path'

export default defineConfig({
  testDir: './e2e', fullyParallel: false,
  outputDir: process.env.PURRAI_TEST_OUTPUT || path.join('test-results', `run-${Date.now()}`),
  use: { baseURL: process.env.PURRAI_TEST_BASE_URL || 'http://127.0.0.1:5174', viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.PURRAI_BROWSER_EXECUTABLE ? { executablePath: process.env.PURRAI_BROWSER_EXECUTABLE } : {} },
  webServer: process.env.PURRAI_TEST_BASE_URL ? undefined : { command: 'npm run dev -- --port 5174', url: 'http://127.0.0.1:5174', reuseExistingServer: false },
  reporter: [['list']],
})
