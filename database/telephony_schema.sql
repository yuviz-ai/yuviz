-- Pluggable telephony provider configuration.
-- Run: psql voiceai -f database/telephony_schema.sql (after database/schema.sql). Idempotent.

-- ── telephony_configs ────────────────────────────────────────────────────────
-- One row per provider account. `credentials` is validated per provider by libs/telephony_sdk.
CREATE TABLE IF NOT EXISTS telephony_configs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           UUID NOT NULL REFERENCES tenants(id),
    name                TEXT NOT NULL,
    provider            TEXT NOT NULL,
    credentials         JSONB NOT NULL DEFAULT '{}',
    is_default_outbound BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);

-- At most one default-outbound config per tenant.
CREATE UNIQUE INDEX IF NOT EXISTS idx_telephony_configs_default_outbound
    ON telephony_configs(tenant_id) WHERE is_default_outbound AND deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_telephony_configs_tenant ON telephony_configs(tenant_id);

-- NULL for DIDs routed purely through Gateway/Kamailio/FreeSWITCH.
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS telephony_config_id UUID REFERENCES telephony_configs(id);

-- Last provider sync outcome ({ok, message, at}); NULL if never synced or nothing to sync.
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS provider_sync JSONB;

-- At most one native (local SIP) config per tenant; it has no credentials to differ on.
DO $$
DECLARE dupes int;
BEGIN
  SELECT count(*) INTO dupes FROM (
    SELECT tenant_id FROM telephony_configs
     WHERE provider = 'native' AND deleted_at IS NULL GROUP BY tenant_id HAVING count(*) > 1
  ) d;
  IF dupes > 0 THEN
    RAISE EXCEPTION '% tenant(s) have more than one live native telephony_config; merge them before applying telephony_schema.sql', dupes;
  END IF;
  EXECUTE $sql$CREATE UNIQUE INDEX IF NOT EXISTS telephony_configs_one_native_per_tenant
    ON telephony_configs (tenant_id) WHERE provider = 'native' AND deleted_at IS NULL$sql$;
END $$;
