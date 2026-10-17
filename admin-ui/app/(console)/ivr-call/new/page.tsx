"use client";

// One-screen IVR-Call creation. Scaffolds are built client-side; clones are resolved server-side
// (clone_from_id) so a clone can't read a flow the caller can't open or cross tenants.

import { useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  ArrowLeft, ArrowLeftRight, ArrowRight, Bot, Copy, Forward, Grid3x3, Hash, ListChecks,
  LucideIcon, Megaphone, PenLine, Phone, PhoneIncoming, PhoneOutgoing, Rocket, User,
} from "lucide-react";
import { ApiError } from "@/lib/api";
import { useActiveTenant } from "@/lib/useActiveTenant";
import { CallFlowGraphNode, CallFlowNodeType, CallFlowSummary, createCallFlow, listCallFlows } from "@/lib/callFlowApi";
import { BLOCKS, FlowDirection, STARTERS, scaffoldGraph } from "@/lib/callFlowScaffold";

const DIRECTIONS: { value: FlowDirection; title: string; blurb: string; icon: LucideIcon }[] = [
  { value: "inbound", title: "People call us", blurb: "Inbound", icon: PhoneIncoming },
  { value: "outbound", title: "We call people", blurb: "Outbound", icon: PhoneOutgoing },
  { value: "both", title: "Both", blurb: "Either direction", icon: ArrowLeftRight },
];

const STARTER_ICON: Record<string, LucideIcon> = {
  press: Grid3x3, agent: Bot, person: User, number: Hash, message: Megaphone,
};

const HEARS: Record<CallFlowNodeType, string> = {
  start: "",
  play: "Plays a message",
  menu: "Plays the options",
  collect: "Asks for a number",
  agent: "Hands over to your AI agent",
  dial: "Transfers to a person",
  hangup: "Ends the call",
};

const BRANCH: Partial<Record<CallFlowNodeType, string>> = {
  agent: "AI agent", dial: "a person", hangup: "end the call", play: "a message", collect: "ask for a number",
};

function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

export default function NewCallFlowPage() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const { tenant, allTenants, isAllTenants, error: tenantError } = useActiveTenant();
  const [accountSlug, setAccountSlug] = useState(searchParams.get("tenant") || "");
  // A superadmin on "All tenants" still has to say which account owns the menu.
  const tenantSlug = (tenant ?? allTenants.find((t) => t.slug === accountSlug) ?? allTenants[0])?.slug ?? "";
  const [flowList, setFlowList] = useState<{ slug: string; flows: CallFlowSummary[] }>({ slug: "", flows: [] });
  const existing = flowList.slug === tenantSlug ? flowList.flows : [];

  const [direction, setDirection] = useState<FlowDirection>("inbound");
  // A starter key, "scratch", or "clone".
  const [start, setStart] = useState("press");
  const [clonePick, setClonePick] = useState<{ slug: string; id: string }>({ slug: "", id: "" });
  const cloneFromId = clonePick.slug === tenantSlug ? clonePick.id : "";
  const [picks, setPicks] = useState<CallFlowNodeType[]>(STARTERS[0].picks);
  const [name, setName] = useState("");

  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (tenantSlug) {
      listCallFlows(tenantSlug)
        .then((flows) => { if (!cancelled) setFlowList({ slug: tenantSlug, flows }); })
        .catch(() => { if (!cancelled) setFlowList({ slug: tenantSlug, flows: [] }); });
    }
    return () => { cancelled = true; };
  }, [tenantSlug]);

  const cloning = start === "clone";
  const blocks = BLOCKS.filter((b) => b.directions.includes(direction));
  // Picks are kept in BLOCKS order so the chain matches the list the user sees.
  const steps = blocks.filter((b) => picks.includes(b.type)).map((b) => b.type);
  const hasMenu = steps.includes("menu");
  const terminalSteps: CallFlowNodeType[] = ["agent", "dial", "hangup"];
  const firstTerminal = !hasMenu ? steps.find((type) => terminalSteps.includes(type)) : undefined;
  const graph = scaffoldGraph(steps, direction);

  // Walk the scaffold: a straight line from start, then the menu's keypress branches.
  const byId = new Map(graph.nodes.map((n) => [n.id, n]));
  const line: CallFlowGraphNode[] = [];
  let lastId = "start";
  for (;;) {
    const outs = graph.edges.filter((e) => e.source === lastId);
    if (outs.length !== 1 || outs[0].data?.key) break;
    lastId = outs[0].target;
    line.push(byId.get(lastId)!);
  }
  const branches = graph.edges
    .filter((e) => e.source === lastId && e.data?.key)
    .map((e) => ({ key: e.data!.key!, type: byId.get(e.target)!.type }));

  const chooseStart = (key: string) => {
    setStart(key);
    const starter = STARTERS.find((s) => s.key === key);
    if (starter) setPicks(starter.picks);
    else if (key === "scratch") setPicks([]);
  };

  const chooseDirection = (next: FlowDirection) => {
    setDirection(next);
    // A clone uses its source graph; the starter picks are irrelevant in copy mode.
    if (start === "clone") return;
    const valid = picks.filter((type) => BLOCKS.some((block) => block.type === type && block.directions.includes(next)));
    const withoutMenu = !valid.includes("menu");
    const first = valid.find((type) => terminalSteps.includes(type));
    const nextPicks = withoutMenu && first
      ? valid.filter((type) => !terminalSteps.includes(type) || type === first)
      : valid;
    if (nextPicks.length !== picks.length) setStart("scratch");
    setPicks(nextPicks);
  };

  const togglePick = (type: CallFlowNodeType) => {
    const next = picks.includes(type) ? picks.filter((t) => t !== type) : [...picks, type];
    const starter = STARTERS.find((s) => s.key === start);
    if (starter && (next.length !== starter.picks.length || next.some((t) => !starter.picks.includes(t)))) setStart("scratch");
    setPicks(next);
  };

  const cloneSource = existing.find((f) => f.id === cloneFromId);
  const dir = DIRECTIONS.find((d) => d.value === direction)!;
  const canCreate = !creating && !!tenantSlug && slugify(name) !== "" && (!cloning || !!cloneSource);

  const handleCreate = async () => {
    if (!canCreate) return;
    setCreating(true);
    setError(null);
    try {
      const flow = await createCallFlow(tenantSlug, {
        slug: slugify(name),
        name: name.trim(),
        direction,
        ...(cloning ? { clone_from_id: cloneFromId } : { graph }),
      });
      router.push(`/ivr-call/${flow.id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
      setCreating(false);
    }
  };

  return (
    <div className="qc">
      <button className="btn btn-ghost btn-sm" onClick={() => router.push("/ivr-call")}>
        <ArrowLeft size={13} /> All IVR-Calls
      </button>

      <div className="card qc-card">
        <h2>Create an IVR-Call</h2>
        <p className="qc-sub">Everything on one screen. Change any word or key on the next screen.</p>

        {isAllTenants && allTenants.length > 1 && (
          <div className="form-group">
            <label className="form-label">Account</label>
            <select className="form-select" value={tenantSlug} onChange={(e) => setAccountSlug(e.target.value)}>
              {allTenants.map((t) => <option key={t.id} value={t.slug}>{t.name}</option>)}
            </select>
          </div>
        )}

        <div className="qc-lbl"><span className="qc-num">1</span>Which calls is it for?</div>
        <div className="qc-toggle three" role="radiogroup" aria-label="Which calls is it for?">
          {DIRECTIONS.map((d) => (
            <button
              key={d.value}
              type="button"
              role="radio"
              aria-checked={direction === d.value}
              className={direction === d.value ? "on" : ""}
              onClick={() => chooseDirection(d.value)}
            >
              <b>{d.title}</b>
              <span>{d.blurb}</span>
            </button>
          ))}
        </div>

        <div className="qc-lbl"><span className="qc-num">2</span>How do you want to start?</div>
        <div className="qc-tpls">
          {STARTERS.map((s) => {
            const Icon = STARTER_ICON[s.key];
            const unavailable = s.picks.some((t) => !blocks.some((b) => b.type === t));
            return (
              <button
                key={s.key}
                type="button"
                className={`qc-tpl${start === s.key ? " on" : ""}`}
                aria-pressed={start === s.key}
                disabled={unavailable}
                title={unavailable ? "Keypad menus only work when people call you" : undefined}
                onClick={() => chooseStart(s.key)}
              >
                <i className="tpl-ico"><Icon size={15} /></i>
                <b>{s.label}</b>
                <span>{s.blurb}</span>
              </button>
            );
          })}
          <button
            type="button"
            className={`qc-tpl blank${start === "scratch" ? " on" : ""}`}
            aria-pressed={start === "scratch"}
            onClick={() => chooseStart("scratch")}
          >
            <i className="tpl-ico"><ListChecks size={15} /></i>
            <b>From scratch</b>
            <span>Tick the steps yourself.</span>
          </button>
          <button
            type="button"
            className={`qc-tpl blank${cloning ? " on" : ""}`}
            aria-pressed={cloning}
            disabled={existing.length === 0}
            title={existing.length === 0 ? "No menus in this account to copy yet" : undefined}
            onClick={() => chooseStart("clone")}
          >
            <i className="tpl-ico"><Copy size={15} /></i>
            <b>Copy an existing menu</b>
            <span>{existing.length === 0 ? "No menus in this account yet." : "Copy one, then edit the copy."}</span>
          </button>
        </div>

        {cloning ? (
          <>
            <div className="qc-lbl">Menu to copy</div>
            <select className="form-select" value={cloneFromId} onChange={(e) => setClonePick({ slug: tenantSlug, id: e.target.value })}>
              <option value="">— pick a menu —</option>
              {existing.map((f) => <option key={f.id} value={f.id}>{f.name} (v{f.config_version})</option>)}
            </select>
            <div className="form-hint">The copy is a separate menu — editing it never touches the original.</div>
          </>
        ) : (
          <>
            <div className="qc-lbl">Steps in this menu <span className="qc-opt">— ticked for you, change anything</span></div>
            <div className="qc-steps">
              {blocks.map((b) => (
                <label key={b.type}>
                  <input type="checkbox" checked={picks.includes(b.type)}
                    disabled={!hasMenu && terminalSteps.includes(b.type) && !!firstTerminal && firstTerminal !== b.type}
                    onChange={() => togglePick(b.type)} />
                  <span>
                    <b>{b.label}</b>
                    <span className="form-hint">{b.blurb}</span>
                  </span>
                </label>
              ))}
            </div>
            <div className="form-hint" style={{ marginTop: 6 }}>
              Steps run in this order. Steps after the keypad menu become its options, and a hang up is
              added if nothing ends the call.
            </div>
            {!hasMenu && firstTerminal && <div className="form-hint">Only one step can end the call without a keypad menu.</div>}
          </>
        )}

        <div className="qc-lbl"><span className="qc-num">3</span>Name</div>
        <input
          className="form-input"
          value={name}
          maxLength={80}
          placeholder="Main line menu"
          onChange={(e) => setName(e.target.value)}
        />
        {name.trim() !== "" && (
          <div className="form-hint">Address: <span className="mono">{slugify(name) || "—"}</span></div>
        )}

        {(error ?? tenantError) && <div className="error-banner" style={{ marginTop: 12 }}>{error ?? tenantError}</div>}

        <div className="qc-foot">
          <span>
            {dir.blurb} · {cloning
              ? `copy of ${cloneSource?.name ?? "a menu"}`
              : `${line.length} step${line.length === 1 ? "" : "s"} in the menu`}
          </span>
          <button className="btn btn-primary btn-sm" onClick={handleCreate} disabled={!canCreate}>
            {creating ? "Creating…" : <>Create menu <ArrowRight size={13} /></>}
          </button>
        </div>
      </div>

      <aside className="qc-side">
        <div className="card qc-preview">
          <div className="qc-side-title">What callers hear</div>
          <div className="qc-agent">
            <div className="qc-avatar"><Phone size={16} /></div>
            <div className="qc-agent-text">
              <b>{name.trim() || "Your IVR-Call"}</b>
              <span>
                <dir.icon size={12} />
                {dir.title}
                {!cloning && <> · {line.length} step{line.length === 1 ? "" : "s"}</>}
              </span>
            </div>
          </div>
          {cloning ? (
            <p className="qc-job" style={{ marginTop: 14 }}>
              {cloneSource ? `Same steps as ${cloneSource.name}.` : "Pick a menu to copy."}
            </p>
          ) : (
            <ol className="pm-steps">
              {line.map((n, i) => (
                <li key={n.id}>
                  <span className="pm-dot">{i + 1}</span>
                  <span>
                    {HEARS[n.type]}
                    {n.data.prompt && <span className="qc-bubble">{n.data.prompt}</span>}
                  </span>
                </li>
              ))}
              {branches.map((b) => (
                <li key={b.key} className="pm-branch">
                  <span className="pm-key">{b.key === "timeout" ? "–" : b.key}</span>
                  <span>
                    {b.key === "timeout" ? "No key pressed" : `Press ${b.key}`} → {BRANCH[b.type] ?? b.type}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>

        <div className="card qc-next">
          <div className="qc-side-title">What happens next</div>
          <ol>
            <li><PenLine size={14} /><span><b>Write what callers hear</b> in your own words.</span></li>
            <li><Forward size={14} /><span><b>Pick where calls go</b> — AI agent or number.</span></li>
            <li><Rocket size={14} /><span><b>Publish it</b> and connect a phone number.</span></li>
          </ol>
        </div>
      </aside>
    </div>
  );
}
