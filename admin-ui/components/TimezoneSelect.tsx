"use client";

import { useMemo } from "react";
import { timezoneLabel, timezoneNames } from "@/lib/timezones";

export function TimezoneSelect({ id, value, onChange }: { id: string; value: string; onChange: (tz: string) => void }) {
  const zones = useMemo(() => {
    const at = new Date();
    return timezoneNames([value]).map((tz) => ({ tz, label: timezoneLabel(tz, at) }));
  }, [value]);
  return (
    <select id={id} className="form-select" value={value} onChange={(e) => onChange(e.target.value)}>
      {zones.map((z) => (
        <option key={z.tz} value={z.tz}>{z.label}</option>
      ))}
    </select>
  );
}
