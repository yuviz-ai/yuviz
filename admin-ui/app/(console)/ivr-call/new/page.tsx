"use client";

// New call flow wizard. Scaffolds are built client-side; clones are resolved server-side
// (clone_from_id) so a clone can't read a flow the caller can't open or cross tenants.

import { useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { ApiError, Tenant, listTenants } from "@/lib/api";
import { CallFlowNodeType, CallFlowSummary, createCallFlow, listCallFlows } from "@/lib/callFlowApi";
import { BLOCKS, scaffoldGraph } from "@/lib/callFlowScaffold";

type Step = "source" | "direction" | "steps" | "name";
type Direction = "inbound" | "outbound" | "both";

const STEPS: { key: Step; label: string }[] = [
  { key: "source", label: "Start from" },
  { key: "direction", label: "Call type" },
  { key: "steps", label: "Steps" },
  { key: "name", label: "Name it" },
];

const DIRECTIONS: { value: Direction; label: string; blurb: string }[] = [
  {
    value: "inbound",
    label: "Inbound",
    blurb: "Someone calls you. The menu answers, plays the options and sends them on.",
  },
  {
    value: "outbound",
    label: "Outbound",
    blurb: "You call them. The menu plays once they pick up — reminders, confirmations, surveys.",
  },
  {
    value: "both",
    label: "Both",
    blurb: "One menu for calls in either direction.",
  },
];

function slugify(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

export default function NewCallFlowPage() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [step, setStep] = useState<Step>("source");
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantSlug, setTenantSlug] = useState(searchParams.get("tenant") || "");
  const [existing, setExisting] = useState<CallFlowSummary[]>([]);

  const [source, setSource] = useState<"standalone" | "clone">("standalone");
  const [cloneFromId, setCloneFromId] = useState<string>("");
  const [direction, setDirection] = useState<Direction>("inbound");
  const [picks, setPicks] = useState<CallFlowNodeType[]>(["play", "menu", "agent"]);
  const [name, setName] = useState("");

  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listTenants()
      .then((ts) => {
        setTenants(ts);
        if (!tenantSlug && ts.length > 0) setTenantSlug(ts[0].slug);
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!tenantSlug) return;
    listCallFlows(tenantSlug).then(setExisting).catch(() => setExisting([]));
  }, [tenantSlug]);

  const blocksForDirection = useMemo(
    () => BLOCKS.filter((b) => b.directions.includes(direction)),
    [direction],
  );

  const togglePick = (type: CallFlowNodeType) =>
    setPicks((p) => (p.includes(type) ? p.filter((t) => t !== type) : [...p, type]));

  const stepIndex = STEPS.findIndex((s) => s.key === step);
  const cloning = source === "clone";
  const canLeaveSource = !!tenantSlug && (!cloning || !!cloneFromId);
  // A clone copies the whole graph, so the step picker is skipped.
  const visibleSteps = cloning ? STEPS.filter((s) => s.key !== "steps") : STEPS;
  const goNext = () => {
    const order = visibleSteps.map((s) => s.key);
    setStep(order[Math.min(order.indexOf(step) + 1, order.length - 1)]);
  };
  const goBack = () => {
    const order = visibleSteps.map((s) => s.key);
    setStep(order[Math.max(order.indexOf(step) - 1, 0)]);
  };

  const handleCreate = async () => {
    const slug = slugify(name);
    if (!slug || !tenantSlug) return;
    setCreating(true);
    setError(null);
    try {
      const flow = await createCallFlow(tenantSlug, {
        slug,
        name: name.trim(),
        direction,
        ...(cloning
          ? { clone_from_id: cloneFromId }
          : { graph: scaffoldGraph(picks, direction) }),
      });
      router.push(`/ivr-call/${flow.id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : String(e));
      setCreating(false);
    }
  };

  return (
    <>
      <div style={{ display: "flex", alignItems: "center", marginBottom: 14 }}>
        <button className="btn btn-ghost btn-sm" onClick={() => router.push("/ivr-call")}>
          <ArrowLeft size={13} /> Cancel
        </button>
      </div>

      <div className="tabs">
        {visibleSteps.map((s, i) => (
          <button
            key={s.key}
            className={`tab${step === s.key ? " active" : ""}`}
            disabled={i > 0 && !canLeaveSource}
            onClick={() => setStep(s.key)}
          >
            {i + 1}. {s.label}
          </button>
        ))}
      </div>

      {error && <div className="error-banner">{error}</div>}

      {step === "source" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Start from</div>
          </div>
          <div className="card-body">
            <div className="form-group">
              <label className="form-label">Account <span className="required">*</span></label>
              <select className="form-select" value={tenantSlug} onChange={(e) => setTenantSlug(e.target.value)}>
                {tenants.map((t) => (
                  <option key={t.id} value={t.slug}>{t.name}</option>
                ))}
              </select>
            </div>

            <div className="agent-template-row" style={{ marginBottom: 14 }}>
              <button
                type="button"
                className="agent-template-card"
                style={source === "standalone" ? { borderStyle: "solid", borderColor: "var(--text-3)" } : undefined}
                onClick={() => setSource("standalone")}
              >
                <div className="agent-template-title">From scratch</div>
                <div className="agent-template-blurb">
                  Build from scratch — pick the steps you want on the next screens.
                </div>
              </button>
              <button
                type="button"
                className="agent-template-card"
                style={source === "clone" ? { borderStyle: "solid", borderColor: "var(--text-3)" } : undefined}
                onClick={() => setSource("clone")}
                disabled={existing.length === 0}
                title={existing.length === 0 ? "No menus in this account to copy yet" : undefined}
              >
                <div className="agent-template-title">Copy an existing menu</div>
                <div className="agent-template-blurb">
                  {existing.length === 0
                    ? "No menus in this account yet."
                    : "Copy another menu in this account, then edit the copy."}
                </div>
              </button>
            </div>

            {cloning && (
              <div className="form-group" style={{ marginBottom: 0 }}>
                <label className="form-label">Menu to copy <span className="required">*</span></label>
                <select className="form-select" value={cloneFromId} onChange={(e) => setCloneFromId(e.target.value)}>
                  <option value="">— pick a menu —</option>
                  {existing.map((f) => (
                    <option key={f.id} value={f.id}>{f.name} (v{f.config_version})</option>
                  ))}
                </select>
                <div className="form-hint">
                  The copy is a separate menu — editing it never touches the original.
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {step === "direction" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Call type</div>
            <div className="card-sub">which calls this menu is for</div>
          </div>
          <div className="card-body">
            <div className="agent-template-row" style={{ marginBottom: 0 }}>
              {DIRECTIONS.map((d) => (
                <button
                  key={d.value}
                  type="button"
                  className="agent-template-card"
                  style={direction === d.value ? { borderStyle: "solid", borderColor: "var(--text-3)" } : undefined}
                  onClick={() => setDirection(d.value)}
                >
                  <div className="agent-template-title">{d.label}</div>
                  <div className="agent-template-blurb">{d.blurb}</div>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {step === "steps" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Steps</div>
            <div className="card-sub">a starting point — everything is editable on the canvas</div>
          </div>
          <div className="card-body">
            {blocksForDirection.map((b) => (
              <label
                key={b.type}
                style={{ display: "flex", gap: 10, alignItems: "flex-start", padding: "8px 0", borderBottom: "1px solid var(--border-2)" }}
              >
                <input
                  type="checkbox"
                  style={{ marginTop: 3 }}
                  checked={picks.includes(b.type)}
                  onChange={() => togglePick(b.type)}
                />
                <span>
                  <span style={{ fontWeight: 600, fontSize: ".8rem" }}>{b.label}</span>
                  <span className="form-hint" style={{ display: "block", marginTop: 2 }}>{b.blurb}</span>
                </span>
              </label>
            ))}
            <div className="form-hint" style={{ marginTop: 12 }}>
              Steps are chained in the order listed above, and anything after a keypad menu becomes
              one of its branches. A menu always ends somewhere — if you pick nothing that ends the
              call, a hang up is added for you.
            </div>
          </div>
        </div>
      )}

      {step === "name" && (
        <div className="card">
          <div className="card-hdr">
            <div className="card-title">Name it</div>
          </div>
          <div className="card-body">
            <div className="form-group" style={{ marginBottom: 0 }}>
              <label className="form-label">Name <span className="required">*</span></label>
              <input
                className="form-input"
                autoFocus
                value={name}
                placeholder="Main line menu"
                onChange={(e) => setName(e.target.value)}
              />
              {name.trim() !== "" && (
                <div className="form-hint">Address: <span className="mono">{slugify(name) || "—"}</span></div>
              )}
              <div className="form-hint" style={{ marginTop: 10 }}>
                {cloning
                  ? `Cloning ${existing.find((f) => f.id === cloneFromId)?.name ?? "a menu"} · ${direction}`
                  : `${direction} · ${picks.length} step${picks.length === 1 ? "" : "s"} picked`}
              </div>
            </div>
          </div>
        </div>
      )}

      <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 14 }}>
        {stepIndex > 0 && (
          <button className="btn btn-ghost btn-sm" onClick={goBack} disabled={creating}><ArrowLeft size={13} /> Back</button>
        )}
        {step !== "name" ? (
          <button className="btn btn-primary btn-sm" onClick={goNext} disabled={!canLeaveSource}>
            Next <ArrowRight size={13} />
          </button>
        ) : (
          <button
            className="btn btn-primary btn-sm"
            onClick={handleCreate}
            disabled={creating || !slugify(name) || !tenantSlug}
          >
            {creating ? "Creating…" : "Create menu"}
          </button>
        )}
      </div>
    </>
  );
}
