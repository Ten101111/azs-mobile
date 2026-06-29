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
    allowedHosts: [".loca.lt", ".lhr.life"],
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "0.0.0.0",
    allowedHosts: [".loca.lt", ".lhr.life"],
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
