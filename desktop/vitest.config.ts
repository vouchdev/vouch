import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['test/**/*.test.{ts,tsx}'],
    // Main-process modules (src/main/) use Node.js built-ins (WebSocket, child_process).
    // Override to 'node' so the tests exercise the same runtime as production Electron main.
    environmentMatchGlobs: [
      ['test/http-client.test.ts', 'node'],
      ['test/vouch-locator.test.ts', 'node'],
      ['test/jsonl-client.test.ts', 'node'],
      ['test/method-gate.test.ts', 'node'],
    ],
  },
})
