import type { NextConfig } from "next";

const isDevelopment = process.env.NODE_ENV === "development";

const nextConfig: NextConfig = {
  ...(isDevelopment ? {} : { output: "export" as const }),
  poweredByHeader: false,
  ...(isDevelopment ? {
    async rewrites() {
      return [{ source: "/api/:path*", destination: "http://127.0.0.1:8000/api/:path*" }];
    },
  } : {}),
};

export default nextConfig;
