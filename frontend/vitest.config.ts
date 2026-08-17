import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    // jsdom, because the behaviour under test is rendering over time -- whether
    // tokens appear as they arrive rather than all at once. That cannot be
    // asserted by calling functions; it needs a component tree.
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.tsx", "src/**/*.test.ts"],
  },
});
