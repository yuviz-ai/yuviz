const FALLBACK_ZONES = [
  "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Europe/London", "Europe/Berlin",
  "America/New_York", "America/Chicago", "America/Los_Angeles", "Australia/Sydney", "UTC",
];

export function timezoneNames(include: string[] = []): string[] {
  const all = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : FALLBACK_ZONES;
  return Array.from(new Set([...include.filter(Boolean), ...all]));
}

export function timezoneLabel(tz: string, at = new Date()): string {
  try {
    const offset = new Intl.DateTimeFormat("en-US", { timeZone: tz, timeZoneName: "shortOffset" })
      .formatToParts(at)
      .find((p) => p.type === "timeZoneName")?.value;
    return `${tz.replace(/_/g, " ")}${offset ? ` (${offset.replace("GMT", "UTC")})` : ""}`;
  } catch {
    return tz;
  }
}

// Milliseconds the zone is ahead of UTC at the given instant.
function zoneOffsetMs(tz: string, instant: number): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(new Date(instant));
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value);
  return Date.UTC(get("year"), get("month") - 1, get("day"), get("hour"), get("minute"), get("second")) - instant;
}

/** A datetime-local value ("YYYY-MM-DDTHH:MM") read as wall-clock time in `tz`. */
export function zonedToUtc(local: string, tz: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(local);
  if (!m) return null;
  try {
    const wall = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
    // Second pass corrects for a DST change between the guess and the answer.
    const first = wall - zoneOffsetMs(tz, wall);
    return new Date(wall - zoneOffsetMs(tz, first));
  } catch {
    return null;
  }
}

/** The current wall-clock time in `tz`, as a datetime-local value. */
export function nowInZone(tz: string, now = Date.now()): string {
  try {
    return new Date(now + zoneOffsetMs(tz, now)).toISOString().slice(0, 16);
  } catch {
    return new Date(now).toISOString().slice(0, 16);
  }
}
