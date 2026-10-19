import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // The Docker image runs the standalone server; local runs and Vercel use the default output.
  ...(process.env.NEXT_OUTPUT === "standalone" ? { output: "standalone" as const } : {}),
  poweredByHeader: false,
};

export default config;
