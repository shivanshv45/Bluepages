import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API runs separately. Override with BLUEPAGES_API_PORT when 8000 is taken.
const API_PORT = process.env.BLUEPAGES_API_PORT ?? "8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${API_PORT}`,
        changeOrigin: true,
      },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
