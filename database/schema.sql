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

-- Invite-based onboarding: widen roles, add team, and re-guard email
-- uniqueness case-insensitively (findings 4, 5, 8).
--
-- DROP and re-ADD folded into one DO block for the same reason as the email
-- guard below (lesson 13): `psql -f` is autocommit-per-statement with no
-- ON_ERROR_STOP, so two bare top-level statements would let the DROP commit
-- and then silently leave the table with no role CHECK at all if the ADD
-- ever failed. One statement means one failure rolls both back together.
DO $$ BEGIN
  EXECUTE 'ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check';
  EXECUTE $sql$ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('superadmin','admin','supervisor','agent','viewer'))$sql$;
END $$;
ALTER TABLE users ADD COLUMN IF NOT EXISTS team TEXT;

-- Email identity (findings 4, 5, 8). `psql -f` runs with no ON_ERROR_STOP,
-- so a failing statement is logged and the script keeps going rather than
-- aborting — meaning ordering two separate statements cannot protect the
-- second from running after the first raises. So the guard, the DROP and
-- the lower-casing UPDATE are folded into one DO block: it is a single
-- statement, atomic under autocommit, so a RAISE here rolls back the DROP
-- and the UPDATE together and nothing is left half-applied (lesson 5).
DO $$
DECLARE collisions int;
BEGIN
  SELECT count(*) INTO collisions FROM (
    SELECT lower(email) FROM users WHERE deleted_at IS NULL
    GROUP BY 1 HAVING count(*) > 1
  ) d;
  IF collisions > 0 THEN
    RAISE EXCEPTION
      'users.email has % case-insensitive duplicate(s) among live rows; '
      'soft-delete or merge the losers before applying schema.sql', collisions;
  END IF;
  -- Drop the case-sensitive column UNIQUE only once the guard above has
  -- passed. If it survived, it would keep a soft-deleted address permanently
  -- un-reinvitable — the exact mismatch with get_user_by_email()'s
  -- `deleted_at IS NULL` filter (users.py:33) that would 500 the accept path
  -- when someone re-invites a departed employee. DDL needs EXECUTE inside
  -- plpgsql.
  EXECUTE 'ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key';
  UPDATE users SET email = lower(email) WHERE email <> lower(email);
END $$;

-- Partial, so it agrees with get_user_by_email()'s predicate on
-- `deleted_at IS NULL`: a soft-deleted address is re-invitable and its ghost
-- row cannot collide with the new one. `get_user_by_email` itself now
-- matches on `lower(email)` (services/config/users.py), so the two agree on
-- case as well. Left as its own statement — if the DO block above raised,
-- this fails too (duplicates still present), but that failure damages
-- nothing: the DROP was the only statement that could leave the table worse
-- than it found it, and that one is now inside the guarded block.
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_idx
  ON users (lower(email)) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS user_invites (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID REFERENCES tenants(id),   -- NULL = superadmin invite
    email        TEXT NOT NULL,                 -- stored already lower-cased
    role         TEXT NOT NULL CHECK (role IN ('superadmin','admin','supervisor','agent','viewer')),
    team         TEXT,
    token_hash   TEXT NOT NULL UNIQUE,          -- sha256 hex of the token
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','revoked')),
    expires_at   TIMESTAMPTZ NOT NULL,
    invited_by   UUID REFERENCES users(id),
    accepted_at  TIMESTAMPTZ,
    accepted_user_id UUID REFERENCES users(id),
    last_sent_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-TENANT, not global (finding 2, round 1). A global index would let
-- Tenant B park a pending invite on an email and permanently block Tenant A
-- from onboarding that person, and would raise a UniqueViolation with no user
-- row whose tenant could be named. Cross-tenant collision is instead caught by
-- the users lookup at send time, which is the only check with a real account
-- behind it. COALESCE gives the NULL (superadmin) scope its own slot.
CREATE UNIQUE INDEX IF NOT EXISTS user_invites_pending_email_idx
  ON user_invites (COALESCE(tenant_id, '00000000-0000-0000-0000-000000000000'::uuid), lower(email))
  WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS user_invites_tenant_idx ON user_invites (tenant_id, status, created_at DESC);

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
-- Workflow call observability (variable extraction / outcome).
ALTER TABLE calls ADD COLUMN IF NOT EXISTS disposition         TEXT;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS nodes_visited       JSONB;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS extracted_variables JSONB;

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
-- Function + trigger replacement is one statement so a mid-apply failure cannot
-- leave agents with no version trigger (psql without ON_ERROR_STOP).
DO $agents_version$
BEGIN
    EXECUTE $fn$
        CREATE OR REPLACE FUNCTION bump_agent_config_version() RETURNS TRIGGER AS $body$
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
        $body$ LANGUAGE plpgsql;
    $fn$;
    EXECUTE 'DROP TRIGGER IF EXISTS agents_version ON agents';
    EXECUTE 'CREATE TRIGGER agents_version BEFORE UPDATE ON agents
             FOR EACH ROW EXECUTE FUNCTION bump_agent_config_version()';
END
$agents_version$;

-- Single source for the starter-graph JSON shape (backfill + parity tests).
-- Keep in lockstep with libs/config_sdk.workflow.starter_graph().
CREATE OR REPLACE FUNCTION starter_graph_sql(
    greeting text,
    system_prompt text,
    tools jsonb DEFAULT '[]'::jsonb,
    knowledge_base_ids jsonb DEFAULT '[]'::jsonb
) RETURNS jsonb
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT jsonb_build_object(
        'version', 1,
        'nodes', jsonb_build_array(
            jsonb_build_object(
                'id', 'global', 'type', 'global',
                'position', '{"x": 330, "y": 0}'::jsonb,
                'data', jsonb_build_object(
                    'name', 'always applies',
                    'prompt', COALESCE(system_prompt, '')
                )
            ),
            jsonb_build_object(
                'id', 'start', 'type', 'start',
                'position', '{"x": 0, "y": 0}'::jsonb,
                'data', jsonb_build_object(
                    'name', 'greeting',
                    'prompt', 'Greet the caller and find out what they need.',
                    'greeting', COALESCE(greeting, ''),
                    'tools', COALESCE(tools, '[]'::jsonb),
                    'knowledge_base_ids', COALESCE(knowledge_base_ids, '[]'::jsonb)
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
    );
$$;

-- Pre-workflow agents: seed a starter graph from greeting/system_prompt (and
-- any enabled tool policies) so live `workflow` is never left NULL. Tools come
-- from policies because Node.tools is default-deny. Knowledge ids are patched
-- later in knowledge_schema.sql (agent_knowledge_bases lives there). Re-runs
-- are no-ops. Wrapped in one DO so a partial failure cannot exit 0 with
-- workflow still NULL, and each backfill writes an audit_log row alongside
-- the bump.
DO $workflow_backfill$
DECLARE
    null_left INT;
BEGIN
    WITH starter AS (
        SELECT
            a.id,
            starter_graph_sql(
                COALESCE(a.greeting, ''),
                COALESCE(a.system_prompt, ''),
                COALESCE((
                    SELECT jsonb_agg(p.tool_name ORDER BY p.tool_name)
                    FROM agent_tool_policies p
                    WHERE p.agent_id = a.id AND p.enabled
                ), '[]'::jsonb)
            ) AS graph
        FROM agents a
        WHERE a.workflow IS NULL
    ),
    updated AS (
        UPDATE agents a
        SET workflow = s.graph, workflow_draft = s.graph
        FROM starter s
        WHERE a.id = s.id
        RETURNING a.id, a.workflow, a.config_version
    )
    INSERT INTO audit_log (entity_type, entity_id, action, new_value)
    SELECT
        'agent_workflow',
        u.id,
        'updated',
        jsonb_build_object(
            'note', 'backfilled starter graph',
            'workflow', u.workflow,
            'config_version', u.config_version
        )
    FROM updated u;

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

    SELECT COUNT(*) INTO null_left FROM agents WHERE workflow IS NULL;
    IF null_left > 0 THEN
        RAISE EXCEPTION
            'workflow backfill left % agent(s) with workflow IS NULL', null_left;
    END IF;
END
$workflow_backfill$;

-- ── Remaining indexes ────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_agents_tenant ON agents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_provider_configs_tenant ON provider_configs(tenant_id);
CREATE INDEX IF NOT EXISTS idx_provider_configs_role_env ON provider_configs(role, environment);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_tenant ON phone_numbers(tenant_id);
CREATE INDEX IF NOT EXISTS idx_carriers_tenant ON carriers(tenant_id);

-- ── Agentic API Task Execution (Tool Execution Service) ─────────────────────
-- All additive: CREATE TABLE/INDEX IF NOT EXISTS, ADD COLUMN IF NOT EXISTS —
-- no destructive DDL, so no DO $$ guard is needed (lessons 10, 13). The
-- tables are new, so no data-normalizing backfill exists to collide with
-- the lower(name) unique index (lesson 5).

-- ── custom_apis — a tenant-registered HTTP API, reachable only via execute_api ──
-- auth_config is REFERENCES ONLY, same discipline as tool_provider_configs.api_key_ref:
--   api_key:  {"key_ref":"env:...","location":"header"|"query","name":"X-Api-Key"}
--   bearer:   {"token_ref":"enc:..."}
--   oauth2_client_credentials:
--             {"token_url":"https://...","client_id_ref":"env:...",
--              "client_secret_ref":"enc:...","scope":"orders.write"}
-- chain_levels is the number of APIs in this API's own chain (leaf = 1); it is
-- recomputed for this row and every transitive dependent inside the same
-- transaction as any params write, and the write is rejected if it would exceed
-- graph.MAX_CHAIN_LEVELS (4) or introduce a cycle.
CREATE TABLE IF NOT EXISTS custom_apis (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                UUID NOT NULL REFERENCES tenants(id),
    name                     TEXT NOT NULL,          -- LLM-facing api_name, snake_case
    description              TEXT NOT NULL,          -- surfaced in execute_api's schema
    endpoint_url             TEXT NOT NULL,
    method                   TEXT NOT NULL CHECK (method IN ('GET','POST','PUT','PATCH','DELETE')),
    body_style               TEXT NOT NULL DEFAULT 'json' CHECK (body_style IN ('json','form')),
    auth_scheme              TEXT NOT NULL DEFAULT 'none'
                                CHECK (auth_scheme IN ('none','api_key','bearer','oauth2_client_credentials')),
    auth_config              JSONB NOT NULL DEFAULT '{}'::jsonb,
    side_effecting           BOOLEAN NOT NULL DEFAULT true,   -- UI defaults false only for GET
    idempotency_header       TEXT,                   -- NULL = downstream accepts no idempotency key
    timeout_ms               INT,                    -- per-step ceiling; NULL = 6000
    sensitive_response_paths JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ["$.customer.ssn", ...]
    success_template         TEXT,                   -- optional deterministic confirmation line
    chain_levels             INT  NOT NULL DEFAULT 1,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at               TIMESTAMPTZ
);
-- Tenant-SCOPED uniqueness: two tenants may both register 'lookup_account', and
-- neither can block the other's name (lesson 3). Partial on deleted_at so a
-- soft-deleted name is reusable.
CREATE UNIQUE INDEX IF NOT EXISTS custom_apis_tenant_name_key
    ON custom_apis (tenant_id, lower(name)) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_custom_apis_tenant ON custom_apis (tenant_id) WHERE deleted_at IS NULL;

-- ── custom_api_params — one row per input, and the declared dependency graph ──
-- Edges ARE the upstream params: the graph is exactly
-- {(custom_api_id -> upstream_api_id) : source='upstream'}. No second edge table,
-- so a declared dependency can never disagree with the value it produces.
-- The FK to custom_apis cannot express "same tenant" — custom_apis.py validates
-- upstream_api_id against the owning row's tenant_id inside the write
-- transaction (AC 10, AC 17).
CREATE TABLE IF NOT EXISTS custom_api_params (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    custom_api_id      UUID NOT NULL REFERENCES custom_apis(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    location           TEXT NOT NULL CHECK (location IN ('body','query','header','path')),
    json_type          TEXT NOT NULL CHECK (json_type IN ('string','number','integer','boolean','object','array')),
    description        TEXT NOT NULL DEFAULT '',
    required           BOOLEAN NOT NULL DEFAULT true,
    source             TEXT NOT NULL CHECK (source IN ('literal','caller','upstream')),
    literal_value      JSONB,
    upstream_api_id    UUID REFERENCES custom_apis(id),
    upstream_json_path TEXT,
    sensitive          BOOLEAN NOT NULL DEFAULT false,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (custom_api_id, name),
    CONSTRAINT custom_api_params_source_shape CHECK (
        (source = 'literal'  AND literal_value IS NOT NULL AND upstream_api_id IS NULL)
     OR (source = 'caller'   AND upstream_api_id IS NULL)
     OR (source = 'upstream' AND upstream_api_id IS NOT NULL AND upstream_json_path IS NOT NULL)),
    CONSTRAINT custom_api_params_no_self_dep CHECK (upstream_api_id IS DISTINCT FROM custom_api_id)
);
CREATE INDEX IF NOT EXISTS idx_custom_api_params_api      ON custom_api_params (custom_api_id);
CREATE INDEX IF NOT EXISTS idx_custom_api_params_upstream ON custom_api_params (upstream_api_id)
    WHERE upstream_api_id IS NOT NULL;

-- ── agent_custom_apis — which of the tenant's APIs this agent may target ─────
-- Mirrors agent_knowledge_bases / agent_tool_policies: no row = not enabled,
-- not a broken default. The execute_api agent_tool_policies row is the master
-- switch and carries timeout_ms / max_chain_depth; this table is the allow-list.
CREATE TABLE IF NOT EXISTS agent_custom_apis (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      UUID NOT NULL REFERENCES agents(id),
    custom_api_id UUID NOT NULL REFERENCES custom_apis(id),
    enabled       BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_id, custom_api_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_custom_apis_agent ON agent_custom_apis (agent_id) WHERE enabled;

-- ── api_chain_runs / api_chain_steps — the operator-facing chain history ─────
-- calls.tenant_id is a slug string on the call path, but these rows are written
-- by an admin-scoped service against real UUIDs, so tenant_id is a UUID FK like
-- every other owned row. UNIQUE (tenant_id, idempotency_key) is what makes a
-- re-invoked chain return its recorded outcome instead of re-executing (AC 15).
--
-- 'rate_limited' is deliberately NOT in this CHECK: admission (executor step
-- 2b) rejects before any api_chain_runs row is ever created, so a run row can
-- never legitimately carry that status. It survives only as a
-- ChainExecuteResponse.chain_status literal for the caller that got rejected
-- pre-insert.
CREATE TABLE IF NOT EXISTS api_chain_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    agent_id        UUID NOT NULL REFERENCES agents(id),
    call_id         TEXT,
    session_id      TEXT,
    turn_id         TEXT,
    tool_call_id    TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    target_api_id   UUID NOT NULL REFERENCES custom_apis(id),
    status          TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ('running','success','partial','failed','timeout',
                                         'invalid_argument','unavailable')),
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    UNIQUE (tenant_id, idempotency_key)
);
-- Tenant-first so the chain-history read (_authorize_chain_runs) is scoped by
-- tenant in the index, not only in the predicate.
CREATE INDEX IF NOT EXISTS idx_api_chain_runs_tenant_session ON api_chain_runs (tenant_id, session_id);
CREATE INDEX IF NOT EXISTS idx_api_chain_runs_tenant  ON api_chain_runs (tenant_id, started_at DESC);

CREATE TABLE IF NOT EXISTS api_chain_steps (
    id                 BIGSERIAL PRIMARY KEY,
    run_id             UUID NOT NULL REFERENCES api_chain_runs(id) ON DELETE CASCADE,
    step_index         INT  NOT NULL,          -- 0 = shallowest upstream
    custom_api_id      UUID NOT NULL REFERENCES custom_apis(id),
    api_name           TEXT NOT NULL,          -- denormalized: survives a rename/delete
    level              INT  NOT NULL,
    session_id         TEXT,                   -- denormalized from the run, for history reads only
    status             TEXT NOT NULL CHECK (status IN ('claimed','success','failed','timeout',
                                                       'skipped','invalid_argument','unavailable')),
    http_status        INT,
    error              TEXT,
    arguments_redacted JSONB,                  -- redaction.py applied BEFORE insert
    response_redacted  JSONB,
    argument_sources   JSONB,                  -- {"order_id":"lookup_order:$.data.id","name":"caller"}
    arguments_hash     TEXT,                   -- '<kid>:<hmac>' — see api_side_effect_claims; it is
                                               -- an HMAC, never a plain hash, because it sits in the
                                               -- same row as arguments_redacted (finding 7)
    side_effecting     BOOLEAN NOT NULL,
    idempotency_key    TEXT,
    duration_ms        INT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, step_index),
    -- A NULL is distinct from every other NULL in a unique index, so a side-
    -- effecting step with a NULL hash would silently escape the claim below
    -- (finding 5). Make it unrepresentable rather than merely unwritten.
    CONSTRAINT api_chain_steps_side_effect_keyed
        CHECK (NOT side_effecting OR arguments_hash IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_api_chain_steps_run ON api_chain_steps (run_id);

-- ── api_side_effect_claims — the AC 15 arbiter, deliberately NOT a partial ──
-- index on api_chain_steps. Two reasons the claim needs its own row:
--   (1) api_chain_steps is append-only history (AC 14) — a legitimate repeat of
--       an identical mutation after the window must not overwrite the earlier
--       step's record, which a unique index on the history table would force.
--   (2) Every claim column can be NOT NULL here, so there is no NULL row that
--       slips past uniqueness (finding 5).
-- Scope is (tenant_id, custom_api_id, arguments_hash) — NOT session_id: the
-- caller whose refund succeeded, hung up and redialled arrives with a brand new
-- session, and that is exactly the duplicate AC 15 exists to stop.
CREATE TABLE IF NOT EXISTS api_side_effect_claims (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id),
    custom_api_id  UUID NOT NULL REFERENCES custom_apis(id),
    arguments_hash TEXT NOT NULL,           -- '<kid>:<hmac>', see executor.py step 6
    run_id         UUID NOT NULL REFERENCES api_chain_runs(id) ON DELETE CASCADE,
    session_id     TEXT NOT NULL,           -- who won it, for the refusal message
    status         TEXT NOT NULL CHECK (status IN ('claimed','success','timeout','released')),
    claimed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, custom_api_id, arguments_hash)
);
CREATE INDEX IF NOT EXISTS idx_api_side_effect_claims_run ON api_side_effect_claims (run_id);

-- ── per-agent chain-depth override ──────────────────────────────────────────
-- NULL = use the platform ceiling (graph.MAX_CHAIN_LEVELS = 4), never 0/disabled
-- — the same contract timeout_ms / max_calls_per_turn already document above.
ALTER TABLE agent_tool_policies ADD COLUMN IF NOT EXISTS max_chain_depth INT;

-- ── Live Calls Monitoring ────────────────────────────────────────────────────
-- Per-tenant channel cap — the only source for the Live Calls utilization KPI.
-- INT + CHECK mirror campaigns.max_concurrent_calls (schema.sql:555), but the
-- column is NULLABLE with NO DEFAULT, deliberately unlike campaigns': a
-- platform-wide DEFAULT 1 would BE the inferred global cap AC13 forbids, just
-- moved from the query into the column. NULL means "not configured yet" and is
-- rendered as a setup prompt, never as a number (see get_live_calls below).
-- No backfill: there is no honest value to backfill from — calls history has no
-- provisioned-channel record, so a computed "observed peak" would be the same
-- fabricated cap under a busier name.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS max_concurrent_calls INT;
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_max_concurrent_calls_check;
ALTER TABLE tenants ADD  CONSTRAINT tenants_max_concurrent_calls_check
    CHECK (max_concurrent_calls IS NULL OR max_concurrent_calls >= 1);

-- Mid-call stage. NULL = never transferred; readers COALESCE to 'ai' so no
-- backfill is needed and no existing writer has to change (lesson 32: every
-- write path, including Conversation's abort paths, already satisfies this).
ALTER TABLE calls ADD COLUMN IF NOT EXISTS live_stage TEXT;
ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_live_stage_check;
ALTER TABLE calls ADD  CONSTRAINT calls_live_stage_check
    CHECK (live_stage IS NULL OR live_stage IN ('ai', 'waiting_for_human', 'human_connected'));

-- Serves both the KPI aggregate and the row list: only live rows are indexed,
-- so the index stays bounded by concurrent calls, not by call history.
CREATE INDEX IF NOT EXISTS idx_calls_live_tenant
    ON calls (tenant_id, started_at DESC) WHERE ended_at IS NULL;

-- Latest-turn snippet lookup per live row. idx_transcript_entries_session
-- (session_id only) would still sort every turn of the call.
CREATE INDEX IF NOT EXISTS idx_transcript_entries_session_turn
    ON transcript_entries (session_id, turn_number DESC);

-- Listen/Barge requests. tenant_id is the slug (matching calls.tenant_id's own
-- "slug reference, not a hard FK" note), and session_id deliberately has NO FK:
-- AC11 requires recording a denial, and a denial's requested session may not
-- exist at all — an FK would turn the audit requirement into a 500 on exactly
-- the path it exists for.
CREATE TABLE IF NOT EXISTS live_call_interventions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     TEXT NOT NULL,
    session_id    TEXT NOT NULL CHECK (length(session_id) <= 200),
    action        TEXT NOT NULL CHECK (action IN ('listen', 'barge')),
    outcome       TEXT NOT NULL CHECK (outcome IN ('granted', 'denied', 'unavailable')),
    detail        TEXT,
    user_id       UUID REFERENCES users(id),
    user_email    TEXT NOT NULL,
    ip_address    INET,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_live_call_interventions_lookup
    ON live_call_interventions (tenant_id, session_id, created_at DESC);
