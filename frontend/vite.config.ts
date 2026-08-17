import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxying keeps the browser on a single origin in development, so the app
    // exercises the same same-origin request path it will use in production
    // behind an ingress -- rather than only ever being tested against a
    // CORS-enabled cross-origin setup.
    proxy: {
      "/chat": "http://localhost:8000",
      "/conversations": "http://localhost:8000",
      "/providers": "http://localhost:8000",
      "/metrics": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
      "/readyz": "http://localhost:8000",
    },
  },
});
