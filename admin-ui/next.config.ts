import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // /agents/* folded into /workflows/* when the agent and its flow became one
  // object (2026-08-30). Permanent, because these were the URLs anyone who
  // used the product before then has bookmarked.
  async redirects() {
    return [
      { source: "/agents", destination: "/workflows", permanent: true },
      { source: "/agents/new", destination: "/workflows?new=1", permanent: true },
      {
        source: "/agents/:tenantSlug/:agentSlug",
        destination: "/workflows/:tenantSlug/:agentSlug/settings",
        permanent: true,
      },
    ];
  },
};

export default nextConfig;
