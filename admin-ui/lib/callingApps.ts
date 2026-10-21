import type { CarrierProvider } from "./api";

export const CALLING_APPS = [
  { key: "twilio", label: "Twilio", description: "Buy and use phone numbers from Twilio." },
  { key: "plivo", label: "Plivo", description: "Buy and use phone numbers from Plivo." },
  { key: "vonage", label: "Vonage", description: "Buy and use phone numbers from Vonage." },
  { key: "cloudonix", label: "Cloudonix", description: "Route calls through your Cloudonix account." },
  { key: "vobiz", label: "Vobiz", description: "Route calls through your Vobiz account." },
  { key: "native", label: "Local phone", description: "Use this platform’s own phone system — no outside account." },
];

export const CARRIER_PROVIDERS: readonly string[] = ["twilio", "plivo", "vonage"] satisfies CarrierProvider[];
export const callingAppLabel = (provider: string) => CALLING_APPS.find((app) => app.key === provider)?.label ?? provider;

export function carrierAccountLabel(value: string, editing: true): string | null;
export function carrierAccountLabel(value: string, editing: false): string | undefined;
export function carrierAccountLabel(value: string, editing: boolean): string | null | undefined {
  return value.trim() || (editing ? null : undefined);
}
