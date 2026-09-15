import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // /agents/* is a real route again. It was folded into /workflows/* when an
  // agent and its flow were treated as one object (2026-08-30); that model is
  // reversed — /agents owns the agent's configuration, /workflows owns the
  // call-flow canvas — so the old redirects are gone rather than inverted
  // (inverting them would make /workflows/{t}/{a} bounce to config and leave
  // the canvas unreachable).
};

export default nextConfig;
