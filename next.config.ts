import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // Required for Cloud Run (standalone output bundles server + deps into .next/standalone)
  output: 'standalone',
};

export default nextConfig;
