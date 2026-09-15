-- One-time operator script for RLS rollout step 4 (design.md "Migration and
-- rollout", task T61). `require_path_tenant_access`'s predicate is
-- `tenant_id IS NULL`, not `role = 'superadmin'` (lesson 24) — so any account
-- that today relies on its role to reach other tenants while still carrying a
-- leftover `tenant_id` loses that access silently at DSN cutover: an empty
-- table in the UI, not an error. Run this BEFORE flipping any service's
-- POSTGRES_DSN to yuviz_app (T62). A non-empty, unreviewed result blocks T62.
--
-- Usage: psql "$POSTGRES_DSN" -f scripts/pre_cutover_account_sweep.sql
--
-- This is a REVIEW step, not a blind fix: every returned row needs a human
-- decision (see the two dispositions below), so there is no single UPDATE
-- that is safe to run unattended.

-- Step 1 — enumerate every account whose ROLE says "platform" but whose
-- TENANT_ID says "scoped". Exactly the design's sweep query.
SELECT id, email, role, tenant_id
  FROM users
 WHERE tenant_id IS NOT NULL
   AND (role = 'superadmin' OR is_service_account)
   AND deleted_at IS NULL;

-- Step 2 — for each row above, the operator picks exactly one disposition:
--
--   (a) Genuine platform actor whose tenant_id is a leftover artifact of how
--       the account was created (provider_configs.py:81 documents this as an
--       existing, known shape). Fix it:
--
--         UPDATE users SET tenant_id = NULL WHERE id = '<row id>';
--
--   (b) Correctly-scoped tenant account that happens to hold role=superadmin
--       (a tenant's own "superadmin" seat, not a platform one) or is a
--       tenant-scoped service account. Leave tenant_id as-is; it keeps its
--       tenant under Tier 2/3 exactly as before.
--
-- There is no default: applying (a) to a row that is actually (b) revokes
-- that tenant's admin instead of granting platform scope, and applying (b)
-- to a row that is actually (a) is the exact bug this sweep exists to catch.
