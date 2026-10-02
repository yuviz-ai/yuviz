-- Retire the Cal.com / Twilio-SMS tool configuration left behind when the
-- calendar and SMS built-ins were removed. The rows are inert; this just
-- cleans up the Tools tab and logs.
--
--   psql "$POSTGRES_DSN" -f scripts/retire_calendar_sms_tools.sql
--
-- Soft-deletes tool_provider_configs; disables agent_tool_policies (no
-- deleted_at column). The undo at the bottom restores both.

\set ON_ERROR_STOP on

BEGIN;

-- 1. Review: exactly what is about to be retired, and for whom.
SELECT
    t.slug            AS tenant,
    tpc.engine,
    tpc.name          AS provider_config,
    count(atp.*)      AS policy_rows
FROM tool_provider_configs tpc
JOIN tenants t ON t.id = tpc.tenant_id
LEFT JOIN agent_tool_policies atp ON atp.tool_provider_config_id = tpc.id
WHERE tpc.deleted_at IS NULL
  AND tpc.engine IN ('cal_com', 'twilio')
GROUP BY t.slug, tpc.engine, tpc.name
ORDER BY t.slug, tpc.engine;

-- 2. Any policy row naming a tool that no longer exists in code. If this
--    returns a tool_name you did NOT expect, stop and investigate before
--    committing — it means something else was removed from the registry.
SELECT tool_name, count(*) AS rows, count(*) FILTER (WHERE enabled) AS still_enabled
FROM agent_tool_policies
WHERE tool_name <> 'execute_api'
GROUP BY tool_name
ORDER BY tool_name;

-- 3. Disable the policy rows first, so nothing can resolve mid-migration.
UPDATE agent_tool_policies
   SET enabled = false
 WHERE tool_name <> 'execute_api'
   AND enabled;

-- 4. Soft-delete the now-unreachable provider configs.
UPDATE tool_provider_configs
   SET deleted_at = now()
 WHERE deleted_at IS NULL
   AND engine IN ('cal_com', 'twilio');

-- 5. What is left live — should be 'toolexec' only.
SELECT engine, count(*) AS live_configs
FROM tool_provider_configs
WHERE deleted_at IS NULL
GROUP BY engine
ORDER BY engine;

COMMIT;

-- ── Undo ────────────────────────────────────────────────────────────────
-- Restores rows retired in the last hour (the window spares older deletes).
-- The tools themselves would also need to come back in code.
--
--   UPDATE tool_provider_configs SET deleted_at = NULL
--    WHERE deleted_at > now() - interval '1 hour';
--   UPDATE agent_tool_policies SET enabled = true
--    WHERE tool_name <> 'execute_api';
