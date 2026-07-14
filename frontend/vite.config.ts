import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // 也可通过代理访问后端（已开启 CORS，直连亦可）
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8123",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
