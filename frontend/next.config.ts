import type { NextConfig } from "next";

const backendUrl = (process.env.BACKEND_URL ?? "http://127.0.0.1:14499").replace(/\/$/, "");

const nextConfig: NextConfig = {
  devIndicators: false,
  experimental: {
    proxyClientMaxBodySize: "110mb",
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl}/api/:path*`,
      },
      {
        source: "/upload",
        destination: `${backendUrl}/upload`,
      },
      {
        source: "/chat/:path*",
        destination: `${backendUrl}/chat/:path*`,
      },
    ];
  },
};

export default nextConfig;
