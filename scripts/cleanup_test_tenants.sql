-- Soft-delete the test-fixture tenants left behind by the RLS test runs.
--
-- Background: services/*/tests fixtures create a tenant per test and their
-- teardown could not delete them (see .sdlc/rls-tenant-isolation/
-- 04-t59-test-suite-finding.md — fixture teardown hit ForeignKeyViolationError
-- once RLS was enforcing, so tenants accumulated). The local dev database
-- reached 872 live tenants, 868 of them fixtures.
--
-- Why it matters beyond tidiness: every admin-ui list page used to issue one
-- request PER TENANT, so 872 accounts meant thousands of HTTP requests and a
-- page full of "TypeError: Failed to fetch". The pages are now scoped to the
-- selected account, but the account switcher is still unusable with 872
-- entries.
--
-- This is a SOFT delete (sets deleted_at). Every API already filters on
-- `deleted_at IS NULL`, so the rows vanish from the product but nothing is
-- destroyed and the undo at the bottom restores them.
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

-- 1. Review: what is about to be soft-deleted, and does any of it hold
--    content? Anything with a non-zero count here is worth a second look
--    before you commit.
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
-- Restores every tenant this script soft-deleted in the last hour. Run it
-- straight away if the counts above were not what you expected; the window
-- keeps it from also resurrecting tenants deleted earlier on purpose.
--
--   UPDATE tenants SET deleted_at = NULL
--    WHERE deleted_at > now() - interval '1 hour';
