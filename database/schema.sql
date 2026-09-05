-- Voice AI Platform PostgreSQL schema — Phase 5
-- Run: psql voiceai -f database/schema.sql
-- Idempotent: safe to run repeatedly against an existing database.

-- ── tenants ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenants (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                    TEXT NOT NULL,
    slug                    TEXT NOT NULL UNIQUE,
    region                  TEXT NOT NULL DEFAULT 'us',
    vad_engine              TEXT,
    vad_onset_ms            INT,
    vad_hold_ms             INT,
    vad_speech_threshold    FLOAT,
    no_speech_timeout_ms    INT,
    stt_timeout_ms          INT,
    llm_timeout_ms          INT,
    -- Overrides CallFsmTimerConfig::transfer_timeout_default (45s) — see
    -- gateway TenantConfig::from_redis, which enforces the same 10s-120s
    -- bounds as the Python side (libs.config_sdk.validate_transfer_timeout_ms).
    transfer_timeout_ms     INT,
    default_stt_config_id   UUID,  -- FK added below, after provider_configs exists
    default_llm_config_id   UUID,
    default_tts_config_id   UUID,
    config_version          INT NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at              TIMESTAMPTZ
);

-- calls.tenant_id already has existing rows using the string 'default' as a
-- fallback — this row makes that fallback resolve to a real tenant.
INSERT INTO tenants (slug, name) VALUES ('default', 'Default Tenant')
    ON CONFLICT (slug) DO NOTHING;

-- ── provider_configs ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS provider_configs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    name                TEXT NOT NULL,
    role                TEXT NOT NULL CHECK (role IN ('stt', 'llm', 'tts')),
    engine              TEXT NOT NULL,
    model               TEXT,
    voice               TEXT,
    language            TEXT,
    region              TEXT,
    environment         TEXT NOT NULL DEFAULT 'prod' CHECK (environment IN ('prod', 'staging', 'dev')),
    api_key_ref         TEXT,  -- reference ONLY — 'enc:...' | 'k8s:...' | 'env:...' — never a real key
    extra               JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);

-- Deferred FKs (tenants -> provider_configs is a circular reference — this is
-- the only order that works). Guarded so re-running this file doesn't error
-- on "constraint already exists".
DO $$ BEGIN
    ALTER TABLE tenants ADD CONSTRAINT tenants_default_stt_fk FOREIGN KEY (default_stt_config_id) REFERENCES provider_configs(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE tenants ADD CONSTRAINT tenants_default_llm_fk FOREIGN KEY (default_llm_config_id) REFERENCES provider_configs(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE tenants ADD CONSTRAINT tenants_default_tts_fk FOREIGN KEY (default_tts_config_id) REFERENCES provider_configs(id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ── agents ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS agents (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id               UUID NOT NULL REFERENCES tenants(id),
    slug                    TEXT NOT NULL,
    name                    TEXT NOT NULL,
    greeting                TEXT NOT NULL DEFAULT '',
    system_prompt           TEXT NOT NULL DEFAULT '',
    goodbye_grace_ms        INT NOT NULL DEFAULT 3000,
    stt_config_id           UUID REFERENCES provider_configs(id),  -- nullable: override tenant default
    llm_config_id           UUID REFERENCES provider_configs(id),
    tts_config_id           UUID REFERENCES provider_configs(id),
    -- Explicit per-agent language override — NULL means "derive from the
    -- STT/TTS provider's own language setting", the behavior that existed
    -- before this column (see libs/config_sdk's MediaInfo/get_runtime_config
    -- resolution order: agent.language > providers.stt.language >
    -- providers.tts.language). Free-text TEXT, not a CHECK-constrained
    -- enum — same "curated list in the UI, not enforced server-side"
    -- posture as provider_configs.engine/model.
    language                TEXT,
    -- Human escalation / transfer — wires into CallFSM::Transferring (gateway/include/session)
    transfer_type           TEXT NOT NULL DEFAULT 'none' CHECK (transfer_type IN ('warm', 'cold', 'none')),
    transfer_destination    TEXT,
    queue_id                TEXT,
    escalation_threshold    INT,  -- consecutive guardrail triggers before auto-escalating
    -- What caller ID the human agent sees on a warm transfer's agent leg
    -- (cold transfer has no equivalent — uuid_transfer never originates a
    -- new leg, so there's no caller-id parameter to set). Resolved
    -- entirely in the Conversation Service (transfer_engine.py) into a
    -- single caller_id string sent on the TransferRequest gRPC message —
    -- the gateway never sees the policy, only the final value.
    caller_id_policy        TEXT NOT NULL DEFAULT 'original'
                                CHECK (caller_id_policy IN ('original', 'platform', 'custom')),
    platform_did            TEXT,  -- used when caller_id_policy = 'platform'
    custom_caller_id        TEXT,  -- used when caller_id_policy = 'custom'
    -- What the caller experiences while a warm transfer's agent leg is
    -- ringing (cold transfer has no equivalent — the caller's own leg is
    -- redirected immediately, there is no waiting period). Unlike
    -- caller_id_policy above, this is a telephony-execution detail (does
    -- the gateway issue uuid_hold or not), not a business decision — the
    -- raw value rides the TransferRequest gRPC message unresolved, and
    -- WarmTransferCoordinator itself switches on it.
    transfer_waiting_experience TEXT NOT NULL DEFAULT 'announcement_moh'
                                CHECK (transfer_waiting_experience IN ('announcement_moh', 'announcement_silence')),
    -- Optional condition-clause overrides for the LLM's [[END_CALL]]/[[TRANSFER]]
    -- trigger instructions (NULL/empty = built-in defaults). Only the *condition*
    -- is configurable — the token mechanics are fixed and appended by
    -- pipeline.py, so a custom prompt can't break directive parsing.
    end_call_prompt         TEXT,
    transfer_prompt         TEXT,
    -- Optional exact scripted lines the agent SPEAKS at those moments
    -- (synthesized deterministically by pipeline.py, not LLM-generated).
    -- NULL/empty = the LLM chooses its own wording, as before.
    farewell_message        TEXT,
    transfer_announcement   TEXT,
    -- Admin-configured hard ceiling on how long a caller may stay on this
    -- agent, in seconds. NULL = unlimited (the pre-existing behavior — a
    -- call could run forever, bounded only by the caller/agent choosing to
    -- end it). Enforced by services/conversation/pipeline.py: checked
    -- per-turn (after STT, before the LLM call) against wall-clock time
    -- since the call started; when exceeded, the pipeline skips the LLM,
    -- speaks a fixed wrap-up line, and ends the call the same way
    -- [[END_CALL]]/farewell_message do — see libs/config_sdk's
    -- Policies.max_call_duration_s (already modeled there before this
    -- column existed).
    max_call_duration_s     INT CHECK (max_call_duration_s IS NULL OR max_call_duration_s BETWEEN 30 AND 7200),
    -- Reversible "temporarily stop routing calls to this agent" — distinct
    -- from deleted_at (soft-delete): an inactive agent's row/history/config
    -- stays fully intact and editable, it just resolves like an unavailable
    -- agent (same three-tier fallback phone_numbers.status already uses:
    -- fallback_agent_id, then 'default').
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    config_version          INT NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at              TIMESTAMPTZ,
    UNIQUE (tenant_id, slug)
);

-- CREATE TABLE IF NOT EXISTS above doesn't add a column to an agents table
-- that already existed before this file gained `status` — keeps the file
-- safe to re-run against a DB from before that point.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
DO $$ BEGIN
    ALTER TABLE agents ADD CONSTRAINT agents_status_check CHECK (status IN ('active', 'inactive'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS language TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS caller_id_policy TEXT NOT NULL DEFAULT 'original';
DO $$ BEGIN
    ALTER TABLE agents ADD CONSTRAINT agents_caller_id_policy_check
        CHECK (caller_id_policy IN ('original', 'platform', 'custom'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS platform_did TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS custom_caller_id TEXT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS transfer_waiting_experience TEXT NOT NULL DEFAULT 'announcement_moh';
DO $$ BEGIN
    ALTER TABLE agents ADD CONSTRAINT agents_transfer_waiting_experience_check
        CHECK (transfer_waiting_experience IN ('announcement_moh', 'announcement_silence'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS max_call_duration_s INT;
DO $$ BEGIN
    ALTER TABLE agents ADD CONSTRAINT agents_max_call_duration_s_check
        CHECK (max_call_duration_s IS NULL OR max_call_duration_s BETWEEN 30 AND 7200);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Draft autosave (may be invalid) vs published live graph.
-- Existing rows are backfilled to a starter graph after agent_workflow_versions
-- and the agents_version trigger exist (see below) — never left NULL forever.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS workflow       JSONB;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS workflow_draft JSONB;

-- ── tool_provider_configs — Tool Execution Framework, mirrors provider_configs ──
-- Same shape/discipline as provider_configs above: tenant-scoped, api_key_ref
-- is a REFERENCE ONLY ('env:...' | 'enc:...' | 'k8s:...'), never a real key
-- (see services/conversation/secret_resolver.py's CompositeSecretResolver,
-- reused unchanged for tools). `extra` holds engine-specific settings — for
-- engine='cal_com': event_type_id (one event type per agent by design — see
-- the Tool Execution Framework architecture doc, "intent -> event_type
-- mapping" is an explicit v2+ deferral, not v1 scope) and
-- default_attendee_email (the configured fallback so a voice caller isn't
-- asked for an email unless Cal.com's event type requires one AND no
-- default is configured).
CREATE TABLE IF NOT EXISTS tool_provider_configs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    name                TEXT NOT NULL,
    tool_name           TEXT NOT NULL,  -- matches a static ToolRegistry key, e.g. 'book_appointment'
    engine              TEXT NOT NULL,  -- e.g. 'cal_com'
    api_key_ref         TEXT,
    extra               JSONB,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);

-- ── agent_tool_policies — which tools an agent may actually call ────────────
-- A tool existing in tool_provider_configs does not mean every agent may use
-- it — this is the allow-list, one row per (agent, tool). Mirrors
-- agent_retrieval_policies' "no row = tool not enabled, not a broken
-- default" posture. timeout_ms/max_calls_per_turn are per-tool overrides of
-- the framework defaults (tool_timeout_ms=6000, max_tool_iterations=2) —
-- NULL means "use the framework default", not zero/disabled.
CREATE TABLE IF NOT EXISTS agent_tool_policies (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id                 UUID NOT NULL REFERENCES agents(id),
    tool_name                TEXT NOT NULL,
    tool_provider_config_id  UUID NOT NULL REFERENCES tool_provider_configs(id),
    enabled                  BOOLEAN NOT NULL DEFAULT true,
    timeout_ms               INT,
    max_calls_per_turn       INT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, tool_name)
);

CREATE INDEX IF NOT EXISTS idx_agent_tool_policies_agent ON agent_tool_policies(agent_id) WHERE enabled;

-- ── users ────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID REFERENCES tenants(id),  -- NULL = superadmin (Platform Admin)
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('superadmin', 'admin', 'viewer')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at    TIMESTAMPTZ
);
-- password_hash added after the table's initial design (2026-07-14) — this
-- repo had zero real auth before now (see services/config/auth.py). Every
-- existing users row predates this column, so it has no default: a fresh
-- CREATE TABLE gets NOT NULL immediately, but re-running this file against a
-- DB with pre-auth user rows needs those rows fixed up (re-created via
-- scripts/create_superadmin.py) rather than silently left unusable.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
-- Safe to tighten to NOT NULL only once every existing row has a real hash
-- (true for this repo today — the table was empty pre-auth). A DB with
-- real legacy rows would need those backfilled before this line can run.
ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL;

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_service_account BOOLEAN NOT NULL DEFAULT false;
UPDATE users SET is_service_account = true WHERE lower(email) LIKE '%@internal.%' AND is_service_account = false;

-- Append-only publish history (rollback republishes as a new version).
CREATE TABLE IF NOT EXISTS agent_workflow_versions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id     UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    version      INT  NOT NULL,
    graph        JSONB NOT NULL,
    published_by UUID REFERENCES users(id),
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note         TEXT,
    UNIQUE (agent_id, version)
);
CREATE INDEX IF NOT EXISTS idx_awv_agent ON agent_workflow_versions(agent_id, version DESC);

-- ── audit_log — append-only, written in the same transaction as the mutation ─
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id   UUID NOT NULL,
    user_id     UUID REFERENCES users(id),
    user_email  TEXT,
    action      TEXT NOT NULL CHECK (action IN ('created', 'updated', 'deleted')),
    old_value   JSONB,  -- api_key_ref / auth_token_ref MUST be redacted before writing
    new_value   JSONB,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip_address  INET
);
CREATE INDEX IF NOT EXISTS audit_log_entity_idx ON audit_log(entity_type, entity_id, changed_at DESC);

-- ── carriers — BYOC (bring your own carrier) ─────────────────────────────────
CREATE TABLE IF NOT EXISTS carriers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    name            TEXT NOT NULL,
    provider        TEXT NOT NULL CHECK (provider IN ('twilio', 'plivo', 'vonage')),
    auth_id         TEXT,
    auth_token_ref  TEXT,  -- reference ONLY — same *_ref + SecretResolver pattern as provider_configs
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);

-- ── phone_numbers — DID routing ──────────────────────────────────────────────
-- The number itself is always issued by a carrier; this table is purely the
-- routing decision (which DID -> which tenant/agent). FreeSWITCH's dialplan
-- is a single generic, permanent extension (matches any realistic DID
-- pattern, never edited on tenant/agent onboarding) — it forwards the dialed
-- number to the Gateway as-is; the Gateway resolves it via
-- PhoneRoute::from_redis("did:" + destination_number), reading a cache-aside
-- Redis key this table's services/config/phone_numbers.py populates.
-- mod_xml_curl (a dynamic per-call dialplan fetch) was considered and
-- deliberately rejected for this: the DID carries no information FreeSWITCH
-- itself needs to act on, so there's nothing for a dynamic dialplan to
-- decide — see project memory for the full reasoning.
CREATE TABLE IF NOT EXISTS phone_numbers (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    did                TEXT NOT NULL UNIQUE,
    tenant_id          UUID NOT NULL REFERENCES tenants(id),
    agent_id           UUID REFERENCES agents(id),  -- nullable: unassigned number
    -- Used when agent_id is null or its agent is unavailable/deleted —
    -- resolved by services/config/phone_numbers.py's get_by_did(), not by
    -- the Gateway (which only ever sees the already-resolved agent_slug).
    fallback_agent_id  UUID REFERENCES agents(id),
    carrier_id         UUID REFERENCES carriers(id),
    status             TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'inactive', 'suspended')),
    region             TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at         TIMESTAMPTZ
);

-- CREATE TABLE IF NOT EXISTS above doesn't add columns to a phone_numbers
-- table that already existed before this file gained fallback_agent_id/
-- status — these keep the file safe to re-run against a DB from before that
-- point, matching the file's own "safe to re-run any number of times" goal.
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS fallback_agent_id UUID REFERENCES agents(id);
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
DO $$ BEGIN
    ALTER TABLE phone_numbers ADD CONSTRAINT phone_numbers_status_check CHECK (status IN ('active', 'inactive', 'suspended'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- carrier_account_ref (2026-07-23, DID Management platform): the carrier's
-- own account identifier (e.g. Plivo Auth ID / Twilio Account SID) needed
-- alongside auth_token_ref to make authenticated Numbers API calls — not
-- secret itself, but kept beside auth_token_ref rather than reusing auth_id
-- (which already means something else here: SIP trunk auth, not REST API
-- auth) to avoid conflating the two.
ALTER TABLE carriers ADD COLUMN IF NOT EXISTS carrier_account_ref TEXT;

-- ── purchased_numbers — DID Service's own purchase-lifecycle record ──────────
-- Deliberately NOT part of phone_numbers (see project memory
-- phone-numbers-schema-boundaries): a purchased number may exist for a
-- while before anyone assigns it to an agent, and "which carrier
-- account/carrier-side id bought this" is purchase-lifecycle metadata, not
-- DID->agent routing. phone_number_id is set only once an admin assigns
-- the number via Config Service's existing phone_numbers endpoint — DID
-- Service never writes to phone_numbers directly (see
-- did-management-platform-architecture principle #7).
CREATE TABLE IF NOT EXISTS purchased_numbers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    carrier_id          UUID NOT NULL REFERENCES carriers(id),
    phone_number        TEXT NOT NULL UNIQUE,
    carrier_number_sid  TEXT NOT NULL,  -- carrier's own id for this number, needed to release() it later
    phone_number_id     UUID REFERENCES phone_numbers(id),  -- set once assigned to an agent
    purchased_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_purchased_numbers_tenant ON purchased_numbers(tenant_id) WHERE released_at IS NULL;

-- ── calls — one row per phone call ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS calls (
    session_id            TEXT        PRIMARY KEY,
    tenant_id              TEXT        NOT NULL DEFAULT 'default',  -- slug reference, not a hard FK (see note below)
    call_id                TEXT,
    gateway_node           TEXT,
    conv_node              TEXT,
    started_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at               TIMESTAMPTZ,
    duration_ms            INTEGER,
    close_reason           TEXT,
    turn_count             INTEGER     DEFAULT 0,
    barge_in_count         INTEGER     DEFAULT 0,
    final_state            TEXT,
    agent_id               UUID REFERENCES agents(id),
    agent_config_version   INT,
    caller_number          TEXT,
    called_number          TEXT,
    answered_at            TIMESTAMPTZ,
    recording_ref          TEXT,
    region                 TEXT
);
-- calls.tenant_id stays a TEXT slug (matching tenants.slug) rather than a UUID
-- FK to tenants.id — existing rows/callers already use the literal string
-- 'default' as a fallback; tightening this to a hard FK is a data-migration
-- decision, not required to unblock Phase 5.

-- ── conversation_node_heartbeats — dead-node detection ───────────────────
-- Added 2026-07-29. Each Conversation Service process UPSERTs its own row
-- every HEARTBEAT_INTERVAL_S (see transcript_builder.py). Lets
-- reconcile_dead_nodes() close out calls owned by an instance that's gone
-- silent (crashed, never coming back under the same node_id) WITHOUT
-- waiting for that exact node_id to restart — the gap the earlier
-- startup-only reconcile_stale_calls() couldn't cover on its own.
CREATE TABLE IF NOT EXISTS conversation_node_heartbeats (
    node_id      TEXT PRIMARY KEY,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- direction was already threaded through the gRPC SessionContext/proto
-- (Gateway -> Converse -> SessionOpenRequest.direction) but never actually
-- persisted here — TranscriptBuilder.begin_call() silently dropped it.
-- Default 'inbound' matches the Gateway's own CallMetadata default and the
-- only path that exists today (no outbound/campaign calling yet).
ALTER TABLE calls ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'inbound';
-- 'test' added 2026-07-27 for the webcall bridge (services/webcall/) — a
-- browser-mic test session against a specific agent, no telephony
-- involved at all. Distinct from inbound/outbound so call history/
-- analytics never mistake a test session for a real call.
ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_direction_check;
ALTER TABLE calls ADD CONSTRAINT calls_direction_check CHECK (direction IN ('inbound', 'outbound', 'test'));

CREATE INDEX IF NOT EXISTS idx_calls_tenant   ON calls(tenant_id);
CREATE INDEX IF NOT EXISTS idx_calls_started  ON calls(started_at);
CREATE INDEX IF NOT EXISTS idx_calls_agent    ON calls(agent_id);

-- ── transcript_entries — one row per round-trip (caller_text + ai_response) ──
-- Kept as one row per round-trip, not one row per individual message, to match
-- TranscriptBuilder's existing record_turn() call shape (services/conversation/
-- transcript_builder.py) — splitting into per-message rows would require
-- rewriting that call shape, a logic change rather than a schema extension.
CREATE TABLE IF NOT EXISTS transcript_entries (
    id                  BIGSERIAL   PRIMARY KEY,
    session_id          TEXT        NOT NULL REFERENCES calls(session_id),
    turn_number         INTEGER     NOT NULL,
    caller_text         TEXT,
    caller_confidence   FLOAT,
    ai_response         TEXT,
    interrupted         BOOLEAN     DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stt_engine          TEXT,
    stt_latency_ms      INT,
    llm_engine          TEXT,
    llm_latency_ms      INT,
    tts_engine          TEXT,
    tts_latency_ms      INT,
    latency_ms          JSONB,  -- {"vad_hold":500,"stt":198,"llm":382,"tts":176}
    intent              TEXT,   -- Phase 6a
    entities            JSONB,  -- Phase 6a
    tool_calls          JSONB,  -- Phase 6b
    metadata            JSONB
);

CREATE INDEX IF NOT EXISTS idx_transcript_entries_session ON transcript_entries(session_id);
CREATE INDEX IF NOT EXISTS te_intent_idx ON transcript_entries(intent) WHERE intent IS NOT NULL;
CREATE INDEX IF NOT EXISTS te_entities_idx ON transcript_entries USING GIN(entities) WHERE entities IS NOT NULL;
CREATE INDEX IF NOT EXISTS te_tool_calls_idx ON transcript_entries USING GIN(tool_calls) WHERE tool_calls IS NOT NULL;

-- ── campaigns / campaign_contacts — outbound calling (services/campaigns/) ──
-- Added 2026-07-28, informed by Dograh's real API shape (Create Campaign,
-- Upload Contacts CSV, Start/Pause/Resume, Get Progress) rather than
-- invented from scratch. caller_id is deliberately the tenant's OWN
-- inbound-facing agent DID, not a free-form string — see originate.py's
-- module docstring for why: routing the outbound leg's destination_number
-- to that same DID lets the existing Redis-based inbound tenant/agent
-- resolution resolve it completely unchanged, no Gateway/dialplan code
-- needed. UNVERIFIED end to end (no real trunk/DID to test against yet) —
-- same discipline as providers/plivo.py.
CREATE TABLE IF NOT EXISTS campaigns (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id              UUID NOT NULL REFERENCES tenants(id),
    agent_id               UUID NOT NULL REFERENCES agents(id),
    name                   TEXT NOT NULL,
    status                 TEXT NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft', 'running', 'paused', 'completed')),
    caller_id              TEXT,          -- the agent's own inbound DID — see note above
    max_concurrent_calls   INT NOT NULL DEFAULT 1,
    pacing_seconds         INT NOT NULL DEFAULT 5,  -- min gap between originate attempts
    max_attempts           INT NOT NULL DEFAULT 1,  -- retries for no_answer/failed, per contact
    -- Calling-hours guardrail, added 2026-07-28: NULL/NULL means "no
    -- restriction" (preserves existing campaigns' behavior). "HH:MM" text,
    -- not TIME, to sidestep timezone-vs-TIME type conversion entirely —
    -- the worker parses these against calling_hours_timezone itself.
    calling_hours_start    TEXT,
    calling_hours_end      TEXT,
    calling_hours_timezone TEXT NOT NULL DEFAULT 'UTC',
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at             TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_campaigns_tenant ON campaigns(tenant_id);

CREATE TABLE IF NOT EXISTS campaign_contacts (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id       UUID NOT NULL REFERENCES campaigns(id),
    phone_number      TEXT NOT NULL,
    name              TEXT,
    -- 'blocked' added 2026-07-28 for the do-not-call guardrail — kept
    -- distinct from 'failed' so "we legally couldn't call this" never
    -- looks like "the call attempt failed" in reporting.
    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'calling', 'completed', 'failed', 'no_answer', 'blocked')),
    attempt_count     INT NOT NULL DEFAULT 0,
    last_attempted_at TIMESTAMPTZ,
    call_session_id   TEXT,  -- links to calls.session_id once a call is actually placed
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_campaign_contacts_campaign ON campaign_contacts(campaign_id);
CREATE INDEX IF NOT EXISTS idx_campaign_contacts_pending
    ON campaign_contacts(campaign_id, status) WHERE status = 'pending';

-- ── dnc_numbers — per-tenant do-not-call list (services/campaigns/) ────────
-- Added 2026-07-28. phone_number stored as given at insert time; matching
-- is by last-10-digits (see dnc.py's _normalize_phone, same tolerance-for-
-- formatting approach as tools/providers/calendar/cal_com.py's attendee
-- phone matching) so "+1 415-555-0100" and "4155550100" are recognized as
-- the same number.
CREATE TABLE IF NOT EXISTS dnc_numbers (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    phone_number TEXT NOT NULL,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, phone_number)
);

CREATE INDEX IF NOT EXISTS idx_dnc_numbers_tenant ON dnc_numbers(tenant_id);

-- ── kamailio_cdr — unrelated to Phase 5, unchanged ───────────────────────────
CREATE TABLE IF NOT EXISTS kamailio_cdr (
    id              BIGSERIAL   PRIMARY KEY,
    method          TEXT,
    from_tag        TEXT,
    to_tag          TEXT,
    callid          TEXT,
    sip_code        TEXT,
    sip_reason      TEXT,
    time            TIMESTAMPTZ DEFAULT NOW(),
    duration        INTEGER,
    src_ip          TEXT,
    x_call_id       TEXT
);

-- ── Config versioning trigger — bump on every UPDATE ─────────────────────────
CREATE OR REPLACE FUNCTION bump_config_version() RETURNS TRIGGER AS $$
BEGIN
    NEW.config_version := OLD.config_version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tenants_version ON tenants;
CREATE TRIGGER tenants_version BEFORE UPDATE ON tenants
    FOR EACH ROW EXECUTE FUNCTION bump_config_version();

-- Skip config_version bump when only workflow_draft changed (editor autosave).
CREATE OR REPLACE FUNCTION bump_agent_config_version() RETURNS TRIGGER AS $$
BEGIN
    IF to_jsonb(NEW) - 'workflow_draft' - 'updated_at' - 'config_version'
     = to_jsonb(OLD) - 'workflow_draft' - 'updated_at' - 'config_version'
    THEN
        NEW.config_version := OLD.config_version;
        NEW.updated_at := OLD.updated_at;
        RETURN NEW;
    END IF;
    NEW.config_version := OLD.config_version + 1;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS agents_version ON agents;
CREATE TRIGGER agents_version BEFORE UPDATE ON agents
    FOR EACH ROW EXECUTE FUNCTION bump_agent_config_version();

-- Pre-workflow agents: seed a starter graph from greeting/system_prompt (and
-- any enabled tool policies) so live `workflow` is never left NULL. Mirrors
-- libs/config_sdk.workflow.starter_graph(); re-runs are no-ops.
WITH starter AS (
    SELECT
        a.id,
        jsonb_build_object(
            'version', 1,
            'nodes', jsonb_build_array(
                jsonb_build_object(
                    'id', 'global', 'type', 'global',
                    'position', '{"x": 330, "y": 0}'::jsonb,
                    'data', jsonb_build_object(
                        'name', 'always applies',
                        'prompt', COALESCE(a.system_prompt, '')
                    )
                ),
                jsonb_build_object(
                    'id', 'start', 'type', 'start',
                    'position', '{"x": 0, "y": 0}'::jsonb,
                    'data', jsonb_build_object(
                        'name', 'greeting',
                        'prompt', 'Greet the caller and find out what they need.',
                        'greeting', COALESCE(a.greeting, ''),
                        'tools', COALESCE((
                            SELECT jsonb_agg(p.tool_name ORDER BY p.tool_name)
                            FROM agent_tool_policies p
                            WHERE p.agent_id = a.id AND p.enabled
                        ), '[]'::jsonb)
                    )
                ),
                jsonb_build_object(
                    'id', 'end', 'type', 'end',
                    'position', '{"x": 0, "y": 230}'::jsonb,
                    'data', jsonb_build_object(
                        'name', 'goodbye',
                        'prompt', 'Confirm anything outstanding and close warmly.',
                        'disposition', 'completed'
                    )
                )
            ),
            'edges', jsonb_build_array(
                jsonb_build_object(
                    'id', 'e-start-end', 'source', 'start', 'target', 'end',
                    'data', jsonb_build_object(
                        'label', 'conversation finished',
                        'condition', 'The caller has no further questions.'
                    )
                )
            )
        ) AS graph
    FROM agents a
    WHERE a.workflow IS NULL
)
UPDATE agents a
SET workflow = s.graph, workflow_draft = s.graph
FROM starter s
WHERE a.id = s.id;

-- Draft may still be NULL if only `workflow` was filled by an older path.
UPDATE agents
SET workflow_draft = workflow
WHERE workflow IS NOT NULL AND workflow_draft IS NULL;

INSERT INTO agent_workflow_versions (agent_id, version, graph, note)
SELECT a.id, 1, a.workflow, 'backfilled starter graph'
FROM agents a
WHERE a.workflow IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM agent_workflow_versions v WHERE v.agent_id = a.id
  );

-- ── Remaining indexes ────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_agents_tenant ON agents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_provider_configs_tenant ON provider_configs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_provider_configs_role_env ON provider_configs(role, environment);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_tenant ON phone_numbers(tenant_id);
CREATE INDEX IF NOT EXISTS idx_carriers_tenant ON carriers(tenant_id);
