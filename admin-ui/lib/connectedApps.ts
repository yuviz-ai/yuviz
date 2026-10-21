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
  tools?: { key: string; label: string; configured: boolean }[];
}

type WorkAppDefinition = { key: string; label: string; description: string; category: AppCard["category"]; icon: string; presetKey?: string; comingSoon?: boolean };

const WORK_APPS: WorkAppDefinition[] = [
  { key: "google", label: "Google", description: "Calendar and Sheets your agents can use on calls.", category: "Work", icon: "google" },
  { key: "salesforce", label: "Salesforce", description: "Look up customer records during a call.", category: "Work", icon: "salesforce" },
  { key: "hubspot", label: "HubSpot", description: "Find contacts in your HubSpot CRM.", category: "Work", icon: "hubspot" },
  { key: "zoho", label: "Zoho CRM", description: "Look up contacts in your Zoho CRM.", category: "Work", icon: "zoho" },
  { key: "microsoft", label: "Microsoft", description: "Connect your Microsoft work account.", category: "Work", icon: "microsoft" },
  { key: "calcom", label: "Cal.com", description: "Connect your Cal.com account.", category: "Work", icon: "calcom" },
  { key: "whatsapp_confirmation", label: "WhatsApp", description: "Send a confirmation message after a call.", category: "Messaging", icon: "whatsapp", presetKey: "whatsapp_confirmation" },
  { key: "dynamics_crm", label: "Dynamics 365", description: "Customer records from Microsoft Dynamics.", category: "Work", icon: "dynamics365", presetKey: "dynamics_crm", comingSoon: true },
];

export function availabilityLabel(app: AppCard): string {
  return app.comingSoon ? "Coming soon" : app.calling ? "Temporarily unavailable" : "Needs setup";
}

export function workAppCards(providers: OAuthProvider[], connections: OAuthConnection[], presets: ConnectorPreset[], apis: CustomApi[]): AppCard[] {
  const entries = [...WORK_APPS, ...providers.filter((p) => !WORK_APPS.some((app) => app.key === p.key))
    .map((p) => ({ key: p.key, label: p.label, description: `Use ${p.label} with your agents.` }))];
  const cards: AppCard[] = entries.map((entry) => {
    const catalog = WORK_APPS.find((app) => app.key === entry.key);
    const preset = catalog?.presetKey ? presets.find((p) => p.key === catalog.presetKey) : undefined;
    if (catalog?.presetKey) {
      const rows = apis.filter((a) => a.preset_key === catalog.presetKey);
      return { id: entry.key, label: entry.label, description: entry.description, icon: catalog.icon, category: catalog.category,
        connected: rows.length > 0, reconnect: false, available: !!preset && !catalog.comingSoon,
        comingSoon: catalog.comingSoon, preset, count: rows.length };
    }
    const provider = providers.find((p) => p.key === entry.key);
    const active = connections.filter((c) => c.provider === entry.key && c.status !== "disconnected");
    return { id: entry.key, label: entry.label, description: entry.description, icon: catalog?.icon ?? entry.key, category: "Work",
      available: !!provider, connected: active.some((c) => c.status === "connected"),
      reconnect: active.length > 0 && active.every((c) => c.status === "reconnect_needed"),
      accounts: active, count: active.length, provider,
      tools: presets.filter((preset) => preset.provider === entry.key).map((preset) => ({
        key: preset.key, label: preset.title, configured: apis.some((api) => api.preset_key === preset.key),
      })) };
  });
  // Account tools live in Manage; Sheets also has a shortcut card in the catalog.
  for (const preset of presets.filter((p) => !["calendar_booking", "salesforce_crm", "hubspot_crm", "zoho_crm", ...WORK_APPS.map((app) => app.presetKey).filter(Boolean)].includes(p.key))) {
    const sheets = preset.key === "sheets_lead_capture";
    const rows = apis.filter((a) => a.preset_key === preset.key);
    cards.push({ id: preset.key, label: sheets ? "Google Sheets" : preset.title,
      description: sheets ? "Save leads from calls into a spreadsheet." : `Use ${preset.title} with your agents.`,
      icon: sheets ? "sheets" : preset.provider ?? "calendar",
      category: "Work", connected: rows.length > 0,
      reconnect: rows.length > 0 && !!preset.provider && !connections.some((c) => c.provider === preset.provider && c.status === "connected"
        && rows.some((r) => !r.oauth_connection_id || r.oauth_connection_id === c.id)),
      available: !preset.provider || providers.some((p) => p.key === preset.provider), preset });
  }
  return cards;
}

// Preserve server authorization parameters while asking Google to show its account chooser.
export function oauthAuthorizationUrl(authorizeUrl: string, provider: string, chooseAccount: boolean): string {
  if (provider !== "google" || !chooseAccount) return authorizeUrl;
  const url = new URL(authorizeUrl);
  const prompts = new Set((url.searchParams.get("prompt") ?? "").split(" ").filter(Boolean));
  prompts.add("consent");
  prompts.add("select_account");
  url.searchParams.set("prompt", [...prompts].join(" "));
  return url.href;
}

export function filterApps(cards: AppCard[], query: string, segment: string, category: string): AppCard[] {
  const q = query.trim().toLowerCase();
  return cards.filter((app) => (category === "All" || app.category === category)
    && (segment === "All" || (segment === "Connected" ? app.connected : !app.connected))
    && (!q || `${app.label} ${app.description} ${app.category} ${(app.accounts ?? []).map((a) => a.account_label ?? "").join(" ")}`.toLowerCase().includes(q)));
}
