import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// The backend has no CORS setup, so the dev server forwards /api/* to it.
// Browser calls /api/campaigns -> backend gets /campaigns.
// The target is read from frontend/.env (VITE_API_TARGET); default is a local uvicorn.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const target = env.VITE_API_TARGET || "http://127.0.0.1:8000";
  const port = Number(env.VITE_DEV_PORT) || 5173;
  return {
    plugins: [react()],
    server: {
      port,
      proxy: {
        "/api": {
          target,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
      },
    },
  };
});
