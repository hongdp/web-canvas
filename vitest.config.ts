import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/__tests__/**/*.test.ts'],
    // Explicit rather than implied. Measured: this catches a test awaiting a
    // promise that never resolves (fails at ~5s), but it CANNOT interrupt a
    // synchronous spin — an effect/render cascade starves the event loop, so
    // the runner's own timers never fire and the timeout is only reported
    // after the loop ends. The wall-clock bound in .githooks/pre-push is what
    // covers that case.
    testTimeout: 5_000,
    hookTimeout: 10_000,
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      include: ['src/utils/**', 'src/store/persistence.ts', 'src/store/contentLoader.ts', 'src/store/versionMerge.ts']
    }
  }
})

