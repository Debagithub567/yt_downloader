import type { NextConfig } from "next";

const apiOrigin = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  experimental: {
    // The API accepts files up to 512 MiB; keep the rewrite limit above that cap.
    proxyClientMaxBodySize: 513 * 1024 * 1024,
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  },
};

export default nextConfig;

