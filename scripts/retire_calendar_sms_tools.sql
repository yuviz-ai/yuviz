-- Retire the Cal.com / Twilio-SMS tool configuration left behind when the
-- calendar and SMS built-ins were removed (2026-09-18).
--
-- Background: the agent now has exactly two tools — search_knowledge (RAG,
-- an in-process local tool) and execute_api (every tenant-registered custom
-- API). book_appointment, cancel_appointment, reschedule_appointment and
-- send_sms no longer exist in services/conversation/tools/registry.py, and
-- neither does the cal_com or twilio provider engine.
--
-- These rows are already INERT, not dangerous:
--   * an agent_tool_policies row naming an unknown tool is logged and
--     skipped by ToolPolicyResolver.enabled_tools() — it can never be
--     offered to a model;
--   * a cal_com tool_provider_configs row has no factory left in
--     provider_manager.py, so it can only ever raise "no tool provider
--     factory registered" — and nothing reaches it, because no policy row
--     resolves to it.
-- They are retired here so the Tools tab and the logs stop showing
-- configuration that cannot do anything.
--
--   psql "$POSTGRES_DSN" -f scripts/retire_calendar_sms_tools.sql
--
-- This is a SOFT delete for tool_provider_configs (sets deleted_at, which
-- every read already filters on). agent_tool_policies has no deleted_at
-- column, so those rows are disabled rather than deleted — same effect,
-- still reversible, and it keeps the audit trail of what was once on.
-- The undo at the bottom restores both.

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
-- Restores everything this script retired in the last hour. The window
-- keeps it from also resurrecting configs deleted earlier on purpose.
-- Note that restoring the rows does NOT restore the tools: the registry
-- entries and provider factories would have to come back in code first.
--
--   UPDATE tool_provider_configs SET deleted_at = NULL
--    WHERE deleted_at > now() - interval '1 hour';
--   UPDATE agent_tool_policies SET enabled = true
--    WHERE tool_name <> 'execute_api';
