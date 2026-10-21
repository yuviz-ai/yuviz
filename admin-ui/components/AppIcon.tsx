"use client";

import { useState } from "react";
import { Plug } from "lucide-react";

export function AppIcon({ icon, size = 28 }: { icon: string; size?: number }) {
  const src = `/connected-apps/${icon}.svg`;
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  if (failedSrc === src) return <Plug size={size} aria-hidden="true" />;
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={src} alt="" width={size} height={size} onError={() => setFailedSrc(src)} />;
}
