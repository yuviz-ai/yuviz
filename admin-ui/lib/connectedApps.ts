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
  provider?: OAuthProvider;
  preset?: ConnectorPreset;
  calling?: string;
  count?: number;
}

const WORK_APPS = [
  { key: "google", label: "Google", description: "Calendar and Sheets your agents can use on calls." },
  { key: "microsoft", label: "Microsoft", description: "Connect your Microsoft work account." },
  { key: "salesforce", label: "Salesforce", description: "Look up and update customer records during a call." },
  { key: "calcom", label: "Cal.com", description: "Let callers book a time on your calendar." },
];

export function workAppCards(providers: OAuthProvider[], connections: OAuthConnection[], presets: ConnectorPreset[], apis: CustomApi[]): AppCard[] {
  const entries = [...WORK_APPS, ...providers.filter((p) => !WORK_APPS.some((app) => app.key === p.key))
    .map((p) => ({ key: p.key, label: p.label, description: `Use ${p.label} with your agents.` }))];
  const cards: AppCard[] = entries.map((entry) => {
    const provider = providers.find((p) => p.key === entry.key);
    const active = connections.filter((c) => c.provider === entry.key && c.status !== "disconnected");
    return { id: entry.key, label: entry.label, description: entry.description, icon: entry.key, category: "Work",
      available: !!provider, connected: active.some((c) => c.status === "connected"),
      reconnect: active.some((c) => c.status === "reconnect_needed"), provider };
  });
  // Calendar booking belongs to Google; its actions appear in Manage Google.
  for (const preset of presets.filter((p) => p.key !== "calendar_booking")) {
    const whatsapp = preset.key === "whatsapp_confirmation";
    const sheets = preset.key === "sheets_lead_capture";
    const connected = apis.some((a) => a.preset_key === preset.key);
    cards.push({ id: preset.key, label: whatsapp ? "WhatsApp" : sheets ? "Google Sheets" : preset.title,
      description: whatsapp ? "Send a confirmation message after a call." : sheets ? "Save leads from calls into a spreadsheet." : `Use ${preset.title} with your agents.`,
      icon: whatsapp ? "whatsapp" : sheets ? "sheets" : preset.key === "dynamics_crm" ? "microsoft" : preset.provider ?? "calendar",
      category: whatsapp ? "Messaging" : "Work", connected,
      reconnect: connected && !!preset.provider && !connections.some((c) => c.provider === preset.provider && c.status === "connected"),
      available: !preset.provider || providers.some((p) => p.key === preset.provider), preset });
  }
  if (!cards.some((c) => c.id === "whatsapp_confirmation")) cards.push({ id: "whatsapp_confirmation", label: "WhatsApp", description: "Send a confirmation message after a call.", icon: "whatsapp", category: "Messaging", connected: false, reconnect: false, available: false });
  if (!cards.some((c) => c.id === "dynamics_crm")) cards.push({ id: "dynamics_crm", label: "Dynamics 365", description: "Customer records from Microsoft Dynamics.", icon: "microsoft", category: "Work", connected: false, reconnect: false, available: false });
  return cards;
}

export function filterApps(cards: AppCard[], query: string, segment: string, category: string): AppCard[] {
  const q = query.trim().toLowerCase();
  return cards.filter((app) => (category === "All" || app.category === category)
    && (segment === "All" || (segment === "Connected" ? app.connected : !app.connected))
    && (!q || `${app.label} ${app.description} ${app.category}`.toLowerCase().includes(q)));
}
