import type { NextConfig } from "next";

const apiBase = new URL(process.env.API_BASE_URL ?? "http://127.0.0.1:8100");
if (!['http:', 'https:'].includes(apiBase.protocol) || apiBase.username || apiBase.password || apiBase.search || apiBase.hash || apiBase.pathname !== "/") {
  throw new Error("API_BASE_URL must be an operator-controlled http(s) origin without credentials or paths");
}

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${apiBase.origin}/api/v1/:path*` }];
  },
  async headers() {
    return [{ source: "/:path*", headers: [
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "X-Frame-Options", value: "DENY" },
      { key: "Referrer-Policy", value: "no-referrer" },
      { key: "Cache-Control", value: "no-store" },
    ] }];
  },
};
export default config;
