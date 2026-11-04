import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, API calls are proxied to the FastAPI backend (uvicorn on :8000).
const API = process.env.API_URL ?? "http://127.0.0.1:8000";
const apiPaths = ["/sheets", "/regions", "/fields", "/batches", "/export.csv", "/stats", "/health"];

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(apiPaths.map((p) => [p, { target: API, changeOrigin: true }])),
  },
});
