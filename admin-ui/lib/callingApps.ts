import type { CarrierProvider } from "./api";

export const CALLING_APPS = [
  { key: "twilio", icon: "twilio", label: "Twilio", description: "Buy and use phone numbers from Twilio." },
  { key: "plivo", icon: "plivo", label: "Plivo", description: "Buy and use phone numbers from Plivo." },
  { key: "vonage", icon: "vonage", label: "Vonage", description: "Buy and use phone numbers from Vonage." },
  { key: "cloudonix", icon: "cloudonix", label: "Cloudonix", description: "Route calls through your Cloudonix account." },
  { key: "vobiz", icon: "vobiz", label: "Vobiz", description: "Route calls through your Vobiz account." },
  { key: "native", icon: "local-phone", label: "Local phone", description: "Use this platform’s own phone system — no outside account." },
];

export const CARRIER_PROVIDERS: readonly string[] = ["twilio", "plivo", "vonage"] satisfies CarrierProvider[];
export const callingAppLabel = (provider: string) => CALLING_APPS.find((app) => app.key === provider)?.label ?? provider;

export function carrierAccountLabel(value: string, editing: true): string | null;
export function carrierAccountLabel(value: string, editing: false): string | undefined;
export function carrierAccountLabel(value: string, editing: boolean): string | null | undefined {
  return value.trim() || (editing ? null : undefined);
}

// A stored Account ID can't be cleared; a legacy carrier saved without one stays editable.
export const hasAccountId = (accountId: string, storedAuthId: string | null | undefined, editing: boolean) =>
  !!accountId.trim() || (editing && !storedAuthId);

export function cloudonixCredentials(domain: string, secret: string, existingKeys: unknown[] = []) {
  const replacement = secret.trim();
  return { domain: domain.trim(), api_keys: existingKeys.length
    ? existingKeys.map((_, index) => index === 0 && replacement ? replacement : "[stored]")
    : [replacement] };
}
