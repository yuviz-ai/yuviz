import type { Agent, Campaign, PhoneNumber } from "./api";

// Mirror the agent → default choice used by _load_route. The
// selected agent's status is overridden to model either side of the switch.
function routeFor(agents: Agent[], selected: Agent, selectedActive: boolean) {
  const byId = new Map(agents.map((row) => [row.id, row]));
  byId.set(selected.id, selected);
  const defaultAgent = selected.slug === "default"
    ? selected : agents.find((row) => row.slug === "default");
  const active = (id: string | null): Agent | null => {
    const row = id ? byId.get(id) : null;
    return row && (row.id === selected.id ? selectedActive : row.status === "active") ? row : null;
  };
  return (number: PhoneNumber): Agent | null =>
    active(number.agent_id) ?? active(defaultAgent?.id ?? null);
}

// Phone-shaped identifiers match stored digits-only DIDs; SIP URIs and named
// extensions retain their spelling, as in libs/telephony_sdk/did_format.py.
function normalizeDid(raw: string): string {
  const did = raw.replace(/^[ \t\n\r\f\v]+|[ \t\n\r\f\v]+$/g, "");
  return /^\+?[0-9 ().-]*[0-9][0-9 ().-]*$/.test(did) ? did.replace(/[^0-9]/g, "") : did;
}

export function pauseOutcomes(agent: Agent, agents: Agent[], numbers: PhoneNumber[]): string[] {
  const before = routeFor(agents, agent, true);
  const after = routeFor(agents, agent, false);

  return numbers.flatMap((number) => {
    // A failed later sync does not prove the carrier stopped sending calls.
    if (number.status !== "active") return [];
    const current = before(number);
    if (current?.id !== agent.id && number.agent_id !== agent.id) return [];
    const next = after(number);
    return [next
      ? `${number.did}: calls go to ${next.name}.`
      : `${number.did}: calls won't reach an agent.`];
  });
}

export function resumeMissing(
  agent: Agent, direction: "inbound" | "outbound" | "both",
  agents: Agent[], numbers: PhoneNumber[], campaigns: Campaign[] | null, isChat = false,
): string[] {
  const missing: string[] = [];
  if (isChat) return missing;
  const wired = numbers.filter((number) => number.status === "active" && number.provider_sync?.ok !== false);
  const resumedRoute = routeFor(agents, agent, true);
  if (direction !== "outbound" && !wired.some((number) => resumedRoute(number)?.id === agent.id)) {
    missing.push("No active phone number is assigned to this agent.");
  }
  if (direction !== "inbound") {
    if (campaigns === null) {
      missing.push("Could not check outbound campaign setup.");
    } else {
      const activeDids = new Set(wired.map((number) => normalizeDid(number.did)));
      if (!campaigns.some((campaign) => campaign.agent_id === agent.id && campaign.status !== "completed" &&
        !!campaign.caller_id && activeDids.has(normalizeDid(campaign.caller_id)))) {
        missing.push("No campaign has an active business number to use as caller ID.");
      }
    }
  }
  return missing;
}
