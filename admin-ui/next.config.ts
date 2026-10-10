import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // IVR call menus moved off /workflows (now agents' conversation steps), then off /phone-menus.
  async redirects() {
    return [
      { source: "/workflows", destination: "/ivr-call", permanent: true },
      { source: "/workflows/new", destination: "/ivr-call/new", permanent: true },
      { source: "/workflows/flows/:id", destination: "/ivr-call/:id", permanent: true },
      { source: "/phone-menus/:path*", destination: "/ivr-call/:path*", permanent: true },
    ];
  },
};

export default nextConfig;
