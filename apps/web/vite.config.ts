import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, type ProxyOptions } from "vite";

// The server's own origin. In dev the browser talks to Vite, so proxied
// requests are re-addressed to the server, which refuses foreign Hosts and
// Origins.
const SERVER = "http://127.0.0.1:8787";

const proxy: ProxyOptions = {
  target: SERVER,
  changeOrigin: true,
  configure: (server) => {
    server.on("proxyReq", (req) => {
      if (req.getHeader("origin")) req.setHeader("origin", SERVER);
    });
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    // Served by oa_server at `/`.
    outDir: "../server/src/oa_server/web_dist",
    emptyOutDir: true,
  },
  server: {
    proxy: { "/api": proxy, "/login": proxy },
  },
});
