import { defineConfig } from "vitest/config";

// The library has no build of its own (each app's vite build compiles its source); this
// configures only its tests.
export default defineConfig({
  test: {},
});
