import { defineConfig } from '@playwright/test'

/**
 * End-to-end suite: a real browser against the real backend (FastAPI +
 * SQLite) with only Keitaro and the Five API replaced by in-memory fakes.
 * The webServer command builds the frontend and starts
 * `backend/scripts/e2e_server.py`, which serves both the app and the API on
 * one origin. Tests run serially because they share that server's state and
 * reset it via `/e2e/reset` before each test.
 */
export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:8787',
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command:
      'npm run build && ../backend/.venv/bin/python ../backend/scripts/e2e_server.py --port 8787',
    url: 'http://127.0.0.1:8787/api/v1/health',
    timeout: 180_000,
    reuseExistingServer: false,
  },
})
