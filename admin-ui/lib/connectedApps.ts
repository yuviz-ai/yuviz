import type { ConnectorPreset, CustomApi, OAuthConnection, OAuthProvider } from "./toolexecApi";

export interface AppCard {
  id: string;
  label: string;
  description: string;
  icon: string;
  category: "Calling" | "Work" | "Messaging";
  connected: boolean;
  reconnect: boolean;
  available: boolean;
  comingSoon?: boolean;
  accounts?: OAuthConnection[];
  provider?: OAuthProvider;
  preset?: ConnectorPreset;
  calling?: string;
  count?: number;
}

const WORK_APPS = [
  { key: "google", label: "Google", description: "Calendar and Sheets your agents can use on calls." },
  { key: "salesforce", label: "Salesforce", description: "Look up customer records during a call." },
  { key: "hubspot", label: "HubSpot", description: "Find contacts in your HubSpot CRM." },
  { key: "zoho", label: "Zoho CRM", description: "Look up contacts in your Zoho CRM." },
  { key: "microsoft", label: "Microsoft", description: "Connect your Microsoft work account." },
  { key: "calcom", label: "Cal.com", description: "Connect your Cal.com account." },
];

export function availabilityLabel(app: AppCard): string {
  return app.comingSoon ? "Coming soon" : app.calling ? "Temporarily unavailable" : "Needs setup";
}

export function workAppCards(providers: OAuthProvider[], connections: OAuthConnection[], presets: ConnectorPreset[], apis: CustomApi[]): AppCard[] {
  const entries = [...WORK_APPS, ...providers.filter((p) => !WORK_APPS.some((app) => app.key === p.key))
    .map((p) => ({ key: p.key, label: p.label, description: `Use ${p.label} with your agents.` }))];
  const cards: AppCard[] = entries.map((entry) => {
    const provider = providers.find((p) => p.key === entry.key);
    const active = connections.filter((c) => c.provider === entry.key && c.status !== "disconnected");
    return { id: entry.key, label: entry.label, description: entry.description, icon: entry.key, category: "Work",
      available: !!provider, connected: active.some((c) => c.status === "connected"),
      reconnect: active.length > 0 && active.every((c) => c.status === "reconnect_needed"),
      accounts: active, count: active.length, provider };
  });
  // Account tools live in Manage; Sheets also has a shortcut card in the catalog.
  for (const preset of presets.filter((p) => !["calendar_booking", "salesforce_crm", "hubspot_crm", "zoho_crm"].includes(p.key))) {
    const whatsapp = preset.key === "whatsapp_confirmation";
    const sheets = preset.key === "sheets_lead_capture";
    const rows = apis.filter((a) => a.preset_key === preset.key);
    cards.push({ id: preset.key, label: whatsapp ? "WhatsApp" : sheets ? "Google Sheets" : preset.title,
      description: whatsapp ? "Send a confirmation message after a call." : sheets ? "Save leads from calls into a spreadsheet." : `Use ${preset.title} with your agents.`,
      icon: whatsapp ? "whatsapp" : sheets ? "sheets" : preset.key === "dynamics_crm" ? "dynamics365" : preset.provider ?? "calendar",
      category: whatsapp ? "Messaging" : "Work", connected: rows.length > 0,
      reconnect: rows.length > 0 && !!preset.provider && !connections.some((c) => c.provider === preset.provider && c.status === "connected"
        && rows.some((r) => !r.oauth_connection_id || r.oauth_connection_id === c.id)),
      available: !preset.provider || providers.some((p) => p.key === preset.provider), preset });
  }
  if (!cards.some((c) => c.id === "whatsapp_confirmation")) cards.push({ id: "whatsapp_confirmation", label: "WhatsApp", description: "Send a confirmation message after a call.", icon: "whatsapp", category: "Messaging", connected: false, reconnect: false, available: false });
  if (!cards.some((c) => c.id === "dynamics_crm")) cards.push({ id: "dynamics_crm", label: "Dynamics 365", description: "Customer records from Microsoft Dynamics.", icon: "dynamics365", category: "Work", connected: false, reconnect: false, available: false, comingSoon: true });
  return cards;
}

// Preserve the server-issued state and PKCE values while asking Google to
// show its account chooser when the admin explicitly changes accounts.
export function oauthAuthorizationUrl(authorizeUrl: string, provider: string, chooseAccount: boolean): string {
  if (provider !== "google" || !chooseAccount) return authorizeUrl;
  const url = new URL(authorizeUrl);
  url.searchParams.set("prompt", "consent select_account");
  return url.href;
}

export function filterApps(cards: AppCard[], query: string, segment: string, category: string): AppCard[] {
  const q = query.trim().toLowerCase();
  return cards.filter((app) => (category === "All" || app.category === category)
    && (segment === "All" || (segment === "Connected" ? app.connected : !app.connected))
    && (!q || `${app.label} ${app.description} ${app.category} ${(app.accounts ?? []).map((a) => a.account_label ?? "").join(" ")}`.toLowerCase().includes(q)));
}
