-- Soft-delete the test-fixture tenants left behind by test runs (undo at the bottom).
--
--   psql "$POSTGRES_DSN" -f scripts/cleanup_test_tenants.sql
--
-- Keeps exactly four accounts — the only ones holding anything hand-made:
--   rls-test-a      2 agents, 1 knowledge base, 1 call flow
--   rls-test-b      1 agent
--   my-tenent       1 agent
--   api-chain-test  2 agents
-- Edit this list before running if you want to keep more.

\set ON_ERROR_STOP on

BEGIN;

-- 1. Review: non-zero content counts are worth a second look before committing.
SELECT
    count(*)                                    AS tenants_to_delete,
    count(*) FILTER (WHERE agents  > 0)         AS with_agents,
    count(*) FILTER (WHERE kbs     > 0)         AS with_knowledge_bases,
    count(*) FILTER (WHERE flows   > 0)         AS with_call_flows
FROM (
    SELECT
        (SELECT count(*) FROM agents a          WHERE a.tenant_id = t.id AND a.deleted_at IS NULL) AS agents,
        (SELECT count(*) FROM knowledge_bases k WHERE k.tenant_id = t.id AND k.deleted_at IS NULL) AS kbs,
        (SELECT count(*) FROM call_flows c      WHERE c.tenant_id = t.id AND c.deleted_at IS NULL) AS flows
    FROM tenants t
    WHERE t.deleted_at IS NULL
      AND t.slug NOT IN ('rls-test-a', 'rls-test-b', 'my-tenent', 'api-chain-test')
) s;

-- 2. Soft-delete them.
UPDATE tenants
   SET deleted_at = now()
 WHERE deleted_at IS NULL
   AND slug NOT IN ('rls-test-a', 'rls-test-b', 'my-tenent', 'api-chain-test');

-- 3. What is left live.
SELECT slug, name FROM tenants WHERE deleted_at IS NULL ORDER BY name;

COMMIT;

-- ── Undo ────────────────────────────────────────────────────────────────
-- Restores tenants soft-deleted in the last hour (the window spares older deletions).
--
--   UPDATE tenants SET deleted_at = NULL
--    WHERE deleted_at > now() - interval '1 hour';
