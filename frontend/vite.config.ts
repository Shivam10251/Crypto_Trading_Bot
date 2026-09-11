// defineConfig from vitest/config so the `test` block is typed.
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the backend so the browser sees one origin
// and no CORS handling is needed during development.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
