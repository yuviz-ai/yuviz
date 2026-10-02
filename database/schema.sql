-- Voice AI Platform PostgreSQL schema.
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
    -- Overrides the gateway's 45s transfer timeout; 10s-120s enforced in gateway and config_sdk.
    transfer_timeout_ms     INT,
    default_stt_config_id   UUID,  -- FK added below, after provider_configs exists
    default_llm_config_id   UUID,
    default_tts_config_id   UUID,
    config_version          INT NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at              TIMESTAMPTZ
);

-- Makes calls.tenant_id's 'default' fallback resolve to a real tenant.
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

-- Added after provider_configs because tenants <-> provider_configs is circular.
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
    -- Precedence: agent.language > stt.language > tts.language. NULL = use provider's.
    language                TEXT,
    -- Human escalation / transfer.
    transfer_type           TEXT NOT NULL DEFAULT 'none' CHECK (transfer_type IN ('warm', 'cold', 'none')),
    transfer_destination    TEXT,
    queue_id                TEXT,
    escalation_threshold    INT,  -- consecutive guardrail triggers before auto-escalating
    -- Caller ID on a warm transfer's agent leg; resolved by the Conversation Service.
    caller_id_policy        TEXT NOT NULL DEFAULT 'original'
                                CHECK (caller_id_policy IN ('original', 'platform', 'custom')),
    platform_did            TEXT,  -- used when caller_id_policy = 'platform'
    custom_caller_id        TEXT,  -- used when caller_id_policy = 'custom'
    -- What the caller hears while a warm transfer's agent leg rings; passed raw to the gateway.
    transfer_waiting_experience TEXT NOT NULL DEFAULT 'announcement_moh'
                                CHECK (transfer_waiting_experience IN ('announcement_moh', 'announcement_silence')),
    -- Condition-clause overrides for [[END_CALL]]/[[TRANSFER]]; token mechanics stay fixed.
    end_call_prompt         TEXT,
    transfer_prompt         TEXT,
    -- Spoken verbatim at those moments; NULL/empty = LLM chooses wording.
    farewell_message        TEXT,
    transfer_announcement   TEXT,
    -- Seconds; NULL = unlimited. Checked per turn; on expiry the call is wrapped up and ended.
    max_call_duration_s     INT CHECK (max_call_duration_s IS NULL OR max_call_duration_s BETWEEN 30 AND 7200),
    -- inactive = stop routing calls here (falls back like an unavailable agent); not a delete.
    status                  TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    config_version          INT NOT NULL DEFAULT 1,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at              TIMESTAMPTZ,
    UNIQUE (tenant_id, slug)
);

-- Columns added after the original CREATE TABLE, for existing databases.
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

-- Draft autosave (may be invalid) vs published live graph; backfilled further below.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS workflow       JSONB;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS workflow_draft JSONB;

-- ── tool_provider_configs ─────────────────────────────────────────────────────
-- api_key_ref is a reference only ('env:' | 'enc:' | 'k8s:'), never a real key.
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
-- Per-agent tool allow-list. NULL timeout_ms/max_calls_per_turn = framework default.
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
-- No default: legacy rows without a hash must be re-created before SET NOT NULL succeeds.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL;

ALTER TABLE users ADD COLUMN IF NOT EXISTS is_service_account BOOLEAN NOT NULL DEFAULT false;
UPDATE users SET is_service_account = true WHERE lower(email) LIKE '%@internal.%' AND is_service_account = false;

-- Invite roles. DROP+ADD in one DO block so a failed ADD can't leave no CHECK (psql -f autocommits).
DO $$ BEGIN
  EXECUTE 'ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check';
  EXECUTE $sql$ALTER TABLE users ADD CONSTRAINT users_role_check
    CHECK (role IN ('superadmin','admin','supervisor','agent','viewer'))$sql$;
END $$;
ALTER TABLE users ADD COLUMN IF NOT EXISTS team TEXT;

-- Public signup profile (POST /auth/register). NULL for invited, seeded and
-- Google-created accounts that never filled the signup form.
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_source TEXT;
-- False for Google-created accounts until they choose a password in Settings.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_set BOOLEAN NOT NULL DEFAULT true;
-- Bumped on every password change; tokens carrying an older value are rejected.
ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version INT NOT NULL DEFAULT 0;

-- Signups awaiting their emailed code; no users/tenants row until verified.
-- code_hash is an HMAC, never the code itself.
CREATE TABLE IF NOT EXISTS pending_registrations (
    email             TEXT PRIMARY KEY,  -- lower-cased
    password_hash     TEXT NOT NULL,
    organization_name TEXT NOT NULL,
    first_name        TEXT NOT NULL,
    last_name         TEXT NOT NULL,
    phone             TEXT NOT NULL,
    signup_source     TEXT NOT NULL,
    code_hash         TEXT NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    attempts          INT NOT NULL DEFAULT 0,
    last_sent_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    send_window_start TIMESTAMPTZ NOT NULL DEFAULT now(),
    send_count        INT NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A signed-in user's pending switch to a new address, confirmed by a code
-- sent to that address. One open request per user.
CREATE TABLE IF NOT EXISTS email_change_requests (
    user_id      UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    new_email    TEXT NOT NULL,
    code_hash    TEXT NOT NULL,
    expires_at   TIMESTAMPTZ NOT NULL,
    attempts     INT NOT NULL DEFAULT 0,
    last_sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Forgot-password code emailed to the account's address. One open request per user.
CREATE TABLE IF NOT EXISTS password_reset_requests (
    user_id           UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    code_hash         TEXT NOT NULL,
    expires_at        TIMESTAMPTZ NOT NULL,
    attempts          INT NOT NULL DEFAULT 0,
    last_sent_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    send_window_start TIMESTAMPTZ NOT NULL DEFAULT now(),
    send_count        INT NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Case-insensitive email uniqueness. Guard, DROP and UPDATE share one DO block so they're atomic.
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
  -- The table-wide UNIQUE would block re-inviting a soft-deleted address.
  EXECUTE 'ALTER TABLE users DROP CONSTRAINT IF EXISTS users_email_key';
  UPDATE users SET email = lower(email) WHERE email <> lower(email);
END $$;

-- Partial and lower-cased to match get_user_by_email()'s lookup.
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

-- Per tenant, not global, so one tenant's pending invite can't block another's.
-- COALESCE gives the NULL (superadmin) scope its own slot.
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

-- NULL = platform-scoped. DEFAULT stamps tenant_id from the transaction's app.tenant_id GUC.
-- ON DELETE SET NULL so audit history never blocks a tenant delete.
ALTER TABLE audit_log
    ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL
        DEFAULT NULLIF(current_setting('app.tenant_id', true), '')::uuid;
-- Re-create the FK for databases where the column already existed with RESTRICT.
ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_tenant_id_fkey;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS audit_log_tenant_idx ON audit_log(tenant_id, changed_at DESC);

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
-- DID -> tenant/agent routing. The Gateway reads it via the Redis key "did:<number>".
CREATE TABLE IF NOT EXISTS phone_numbers (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    did                TEXT NOT NULL,  -- unique among live rows only (phone_numbers_did_live_key)
    tenant_id          UUID NOT NULL REFERENCES tenants(id),
    agent_id           UUID REFERENCES agents(id),  -- nullable: unassigned number
    -- Used when agent_id is null or unavailable; resolved by config, not the Gateway.
    fallback_agent_id  UUID REFERENCES agents(id),
    carrier_id         UUID REFERENCES carriers(id),
    status             TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'inactive', 'suspended')),
    region             TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at         TIMESTAMPTZ
);

-- Columns added after the original CREATE TABLE, for existing databases.
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS fallback_agent_id UUID REFERENCES agents(id);
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';
DO $$ BEGIN
    ALTER TABLE phone_numbers ADD CONSTRAINT phone_numbers_status_check CHECK (status IN ('active', 'inactive', 'suspended'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- DIDs are unique among live rows only, so a deleted number can be re-added.
-- Index is created before the old constraint is dropped so uniqueness never lapses.
DO $$
DECLARE dupes int;
BEGIN
  SELECT count(*) INTO dupes FROM (
    SELECT did FROM phone_numbers WHERE deleted_at IS NULL GROUP BY did HAVING count(*) > 1
  ) d;
  IF dupes > 0 THEN
    RAISE EXCEPTION 'phone_numbers has % DID(s) with more than one live row; resolve them before applying schema.sql', dupes;
  END IF;
  EXECUTE 'CREATE UNIQUE INDEX IF NOT EXISTS phone_numbers_did_live_key ON phone_numbers (did) WHERE deleted_at IS NULL';
  EXECUTE 'ALTER TABLE phone_numbers DROP CONSTRAINT IF EXISTS phone_numbers_did_key';
END $$;

-- Carrier REST account id (e.g. Twilio Account SID); auth_id is SIP trunk auth, not this.
ALTER TABLE carriers ADD COLUMN IF NOT EXISTS carrier_account_ref TEXT;

-- ── purchased_numbers — DID Service's own purchase-lifecycle record ──────────
-- Purchase lifecycle, separate from routing. DID Service never writes phone_numbers directly.
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
-- calls.tenant_id is a TEXT slug (tenants.slug), not a UUID FK.

-- Workflow call observability (variable extraction / outcome).
ALTER TABLE calls ADD COLUMN IF NOT EXISTS disposition         TEXT;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS nodes_visited       JSONB;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS extracted_variables JSONB;

-- ── conversation_node_heartbeats — dead-node detection ───────────────────
-- Lets reconcile_dead_nodes() close calls owned by an instance that stopped heartbeating.
CREATE TABLE IF NOT EXISTS conversation_node_heartbeats (
    node_id      TEXT PRIMARY KEY,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE calls ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'inbound';
-- 'test' = webcall browser sessions, kept apart from real calls in analytics.
ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_direction_check;
ALTER TABLE calls ADD CONSTRAINT calls_direction_check CHECK (direction IN ('inbound', 'outbound', 'test'));

-- ── Call sentiment ────────────────────────────────────────────────────────
-- Caller sentiment, scored once after the call (off the live path). NULL = never scored, not neutral.
-- 'frustrated' (with the agent) is distinct from 'negative' (with the answer).
ALTER TABLE calls ADD COLUMN IF NOT EXISTS sentiment        TEXT;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS sentiment_reason TEXT;
ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_sentiment_check;
ALTER TABLE calls ADD CONSTRAINT calls_sentiment_check
    CHECK (sentiment IS NULL OR sentiment IN ('positive', 'neutral', 'negative', 'frustrated'));

CREATE INDEX IF NOT EXISTS idx_calls_tenant   ON calls(tenant_id);
CREATE INDEX IF NOT EXISTS idx_calls_started  ON calls(started_at);
CREATE INDEX IF NOT EXISTS idx_calls_agent    ON calls(agent_id);

-- ── transcript_entries — one row per round-trip (caller_text + ai_response) ──
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
-- caller_id is the agent's own inbound DID so outbound legs reuse inbound DID routing.
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
    -- "HH:MM" in calling_hours_timezone; NULL/NULL = no restriction.
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
    -- 'blocked' = on the do-not-call list, distinct from a failed attempt.
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
-- Stored as given; matched on the last 10 digits (dnc.py _normalize_phone).
CREATE TABLE IF NOT EXISTS dnc_numbers (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    phone_number TEXT NOT NULL,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, phone_number)
);

CREATE INDEX IF NOT EXISTS idx_dnc_numbers_tenant ON dnc_numbers(tenant_id);

-- ── kamailio_cdr ─────────────────────────────────────────────────────────────
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

-- Seed a starter graph for agents with no workflow. Tools come from enabled policies
-- (Node.tools is default-deny); knowledge ids are patched in knowledge_schema.sql.
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

-- ── custom_apis — a tenant-registered HTTP API, reachable only via execute_api ──
-- auth_config holds secret references only:
--   api_key:  {"key_ref":"env:...","location":"header"|"query","name":"X-Api-Key"}
--   bearer:   {"token_ref":"enc:..."}
--   oauth2_client_credentials:
--             {"token_url":"https://...","client_id_ref":"env:...",
--              "client_secret_ref":"enc:...","scope":"orders.write"}
-- chain_levels = APIs in this API's chain (leaf = 1), recomputed on params writes;
-- writes exceeding MAX_CHAIN_LEVELS (4) or creating a cycle are rejected.
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
-- Unique per tenant among live rows.
CREATE UNIQUE INDEX IF NOT EXISTS custom_apis_tenant_name_key
    ON custom_apis (tenant_id, lower(name)) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_custom_apis_tenant ON custom_apis (tenant_id) WHERE deleted_at IS NULL;

-- ── custom_api_params — one row per input, and the declared dependency graph ──
-- source='upstream' rows are the dependency graph edges.
-- Same-tenant upstream_api_id is enforced in custom_apis.py, not by the FK.
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
-- Allow-list; the execute_api agent_tool_policies row is the master switch.
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
-- UNIQUE (tenant_id, idempotency_key) makes a re-invoked chain return its recorded outcome.
-- No 'rate_limited' status: rate-limited requests are rejected before a row exists.
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
    -- A NULL hash would escape the uniqueness claim below.
    CONSTRAINT api_chain_steps_side_effect_keyed
        CHECK (NOT side_effecting OR arguments_hash IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_api_chain_steps_run ON api_chain_steps (run_id);

-- ── api_side_effect_claims — dedupes side-effecting calls ───────────────────
-- Separate from append-only api_chain_steps. Not scoped by session_id, so a redialing
-- caller can't repeat the same mutation.
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
-- NULL = platform ceiling (MAX_CHAIN_LEVELS = 4).
ALTER TABLE agent_tool_policies ADD COLUMN IF NOT EXISTS max_chain_depth INT;

-- ── Live Calls Monitoring ────────────────────────────────────────────────────
-- Per-tenant channel cap for the utilization KPI. No default or backfill on purpose:
-- NULL = not configured.
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS max_concurrent_calls INT;
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_max_concurrent_calls_check;
ALTER TABLE tenants ADD  CONSTRAINT tenants_max_concurrent_calls_check
    CHECK (max_concurrent_calls IS NULL OR max_concurrent_calls >= 1);

-- Mid-call stage. NULL = never transferred; readers COALESCE to 'ai'.
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

-- Listen/Barge requests. session_id has no FK because denials for unknown sessions are recorded too.
CREATE TABLE IF NOT EXISTS live_call_interventions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Slug, not FK: scoping is only correct if a deleted tenant's slug is never reissued.
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

-- ── Call flows (IVR/OBD) ────────────────────────────────────────────────────
-- Standalone keypress-driven flows (libs/config_sdk/callflow.py); one can front several agents.
CREATE TABLE IF NOT EXISTS call_flows (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id),
    slug           TEXT NOT NULL,
    name           TEXT NOT NULL,
    description    TEXT NOT NULL DEFAULT '',
    -- Draft may be invalid; NULL graph = never published.
    graph          JSONB,
    graph_draft    JSONB,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    -- UI hint for the builder and list filter; not enforced.
    direction      TEXT NOT NULL DEFAULT 'inbound'
                       CHECK (direction IN ('inbound', 'outbound', 'both')),
    config_version INT NOT NULL DEFAULT 1,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ,
    UNIQUE (tenant_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_call_flows_tenant ON call_flows (tenant_id) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS call_flow_versions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    call_flow_id UUID NOT NULL REFERENCES call_flows(id) ON DELETE CASCADE,
    version      INT  NOT NULL,
    graph        JSONB NOT NULL,
    published_by UUID REFERENCES users(id),
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    note         TEXT,
    UNIQUE (call_flow_id, version)
);
CREATE INDEX IF NOT EXISTS idx_cfv_flow ON call_flow_versions (call_flow_id, version DESC);

-- Flow answering ahead of this agent; deleting the flow reverts agents to answering directly.
ALTER TABLE agents ADD COLUMN IF NOT EXISTS call_flow_id UUID REFERENCES call_flows(id) ON DELETE SET NULL;

-- agents.call_flow_id must reference a flow in the same tenant (composite FK).
-- Guard, UNIQUE and FK swap share one DO block so they're atomic. NULL is exempt (MATCH SIMPLE).
DO $$
DECLARE violations int;
BEGIN
  SELECT count(*) INTO violations
    FROM agents a JOIN call_flows c ON c.id = a.call_flow_id
   WHERE a.call_flow_id IS NOT NULL AND a.tenant_id <> c.tenant_id;
  IF violations > 0 THEN
    RAISE EXCEPTION
      'agents.call_flow_id has % row(s) pointing at another tenant''s call '
      'flow; fix or NULL them out before applying schema.sql', violations;
  END IF;

  -- Drop the FKs before the UNIQUE they depend on, or re-runs fail.
  EXECUTE 'ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_call_flow_id_fkey';
  EXECUTE 'ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_call_flow_id_tenant_fkey';

  EXECUTE 'ALTER TABLE call_flows DROP CONSTRAINT IF EXISTS call_flows_id_tenant_id_key';
  EXECUTE 'ALTER TABLE call_flows ADD CONSTRAINT call_flows_id_tenant_id_key UNIQUE (id, tenant_id)';

  EXECUTE $sql$ALTER TABLE agents ADD CONSTRAINT agents_call_flow_id_tenant_fkey
    FOREIGN KEY (call_flow_id, tenant_id) REFERENCES call_flows(id, tenant_id) ON DELETE SET NULL$sql$;
END $$;

-- Agent and tenant-default providers must belong to the same tenant.
-- Added only if missing: knowledge_schema.sql's FK depends on provider_configs_id_tenant_id_key.
DO $$
DECLARE violations int;
BEGIN
  SELECT count(*) INTO violations
    FROM agents a
    CROSS JOIN LATERAL (VALUES (a.stt_config_id), (a.llm_config_id), (a.tts_config_id)) ref(provider_id)
    JOIN provider_configs p ON p.id = ref.provider_id
   WHERE p.tenant_id IS DISTINCT FROM a.tenant_id;
  IF violations > 0 THEN
    RAISE EXCEPTION
      'agents has % provider reference(s) to another tenant''s provider_configs row; '
      'point them at the agent''s own tenant''s provider before applying schema.sql', violations;
  END IF;

  SELECT count(*) INTO violations
    FROM tenants t
    CROSS JOIN LATERAL (VALUES (t.default_stt_config_id), (t.default_llm_config_id), (t.default_tts_config_id)) ref(provider_id)
    JOIN provider_configs p ON p.id = ref.provider_id
   WHERE p.tenant_id IS DISTINCT FROM t.id;
  IF violations > 0 THEN
    RAISE EXCEPTION
      'tenants has % default provider reference(s) to another tenant''s provider_configs row; '
      'point them at the tenant''s own provider before applying schema.sql', violations;
  END IF;

  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'provider_configs_id_tenant_id_key') THEN
    EXECUTE 'ALTER TABLE provider_configs ADD CONSTRAINT provider_configs_id_tenant_id_key UNIQUE (id, tenant_id)';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agents_stt_config_tenant_fkey') THEN
    EXECUTE $sql$ALTER TABLE agents ADD CONSTRAINT agents_stt_config_tenant_fkey
      FOREIGN KEY (stt_config_id, tenant_id) REFERENCES provider_configs(id, tenant_id)$sql$;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agents_llm_config_tenant_fkey') THEN
    EXECUTE $sql$ALTER TABLE agents ADD CONSTRAINT agents_llm_config_tenant_fkey
      FOREIGN KEY (llm_config_id, tenant_id) REFERENCES provider_configs(id, tenant_id)$sql$;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'agents_tts_config_tenant_fkey') THEN
    EXECUTE $sql$ALTER TABLE agents ADD CONSTRAINT agents_tts_config_tenant_fkey
      FOREIGN KEY (tts_config_id, tenant_id) REFERENCES provider_configs(id, tenant_id)$sql$;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tenants_default_stt_tenant_fkey') THEN
    EXECUTE $sql$ALTER TABLE tenants ADD CONSTRAINT tenants_default_stt_tenant_fkey
      FOREIGN KEY (default_stt_config_id, id) REFERENCES provider_configs(id, tenant_id)$sql$;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tenants_default_llm_tenant_fkey') THEN
    EXECUTE $sql$ALTER TABLE tenants ADD CONSTRAINT tenants_default_llm_tenant_fkey
      FOREIGN KEY (default_llm_config_id, id) REFERENCES provider_configs(id, tenant_id)$sql$;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'tenants_default_tts_tenant_fkey') THEN
    EXECUTE $sql$ALTER TABLE tenants ADD CONSTRAINT tenants_default_tts_tenant_fkey
      FOREIGN KEY (default_tts_config_id, id) REFERENCES provider_configs(id, tenant_id)$sql$;
  END IF;
END $$;

-- For databases created before call_flows.direction existed.
ALTER TABLE call_flows ADD COLUMN IF NOT EXISTS direction TEXT NOT NULL DEFAULT 'inbound';
ALTER TABLE call_flows DROP CONSTRAINT IF EXISTS call_flows_direction_check;
ALTER TABLE call_flows ADD CONSTRAINT call_flows_direction_check
    CHECK (direction IN ('inbound', 'outbound', 'both'));
