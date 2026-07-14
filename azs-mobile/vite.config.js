import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      filename: "sw.js",
      injectRegister: "script",
      manifest: false,
      registerType: "autoUpdate",
      includeAssets: ["stations.sample.json"],
      workbox: {
        cleanupOutdatedCaches: true,
        clientsClaim: true,
        skipWaiting: true,
        navigateFallback: "/index.html",
        globPatterns: ["**/*.{js,css,html,png,webmanifest}"],
        globIgnores: ["**/icon-source.png", "**/icon.svg"],
        runtimeCaching: [
          {
            urlPattern: ({ url }) => url.pathname === "/stations.sample.json",
            handler: "NetworkFirst",
            options: {
              cacheName: "azs-public-sample",
              expiration: {
                maxEntries: 1,
                maxAgeSeconds: 60 * 60 * 24 * 7,
              },
            },
          },
        ],
      },
    }),
  ],
  server: {
    host: "0.0.0.0",
    allowedHosts: [".loca.lt", ".lhr.life", ".trycloudflare.com", ".ngrok-free.dev", ".ngrok.io", ".ngrok.app"],
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "0.0.0.0",
    allowedHosts: [".loca.lt", ".lhr.life", ".trycloudflare.com", ".ngrok-free.dev", ".ngrok.io", ".ngrok.app"],
    // Security headers for `vite preview`. In production these MUST also be
    // configured on the real static server (nginx / Caddy).
    headers: {
      "X-Content-Type-Options": "nosniff",
      "X-Frame-Options": "DENY",
      "Referrer-Policy": "strict-origin-when-cross-origin",
      "Permissions-Policy": "geolocation=(self), camera=(), microphone=()",
    },
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules/react/") || id.includes("node_modules/react-dom/")) {
            return "vendor-react";
          }
          if (id.includes("node_modules/framer-motion/")) {
            return "vendor-motion";
          }
          if (id.includes("node_modules/d3-array/") || id.includes("node_modules/d3-scale/")) {
            return "vendor-d3";
          }
          if (id.includes("node_modules/lucide-react/")) {
            return "vendor-lucide";
          }
        },
      },
    },
  },
});
