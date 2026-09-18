import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Solo afecta a `next dev`: el login local de Supabase vuelve a 127.0.0.1, y
  // sin esto Next bloquea sus recursos de desarrollo desde ese host.
  allowedDevOrigins: ["127.0.0.1"],
  /* config options here */
};

export default nextConfig;
