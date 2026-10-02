-- Run BEFORE switching POSTGRES_DSN to yuviz_app: platform scope is
-- `tenant_id IS NULL`, not role, so these accounts silently lose cross-tenant access.
-- Usage: psql "$POSTGRES_DSN" -f scripts/pre_cutover_account_sweep.sql
-- Review step only: every returned row needs a human decision.

-- Step 1 — accounts whose role says "platform" but tenant_id says "scoped".
SELECT id, email, role, tenant_id
  FROM users
 WHERE tenant_id IS NOT NULL
   AND (role = 'superadmin' OR is_service_account)
   AND deleted_at IS NULL;

-- Step 2 — for each row above, the operator picks exactly one disposition:
--
--   (a) Genuine platform actor with a leftover tenant_id. Fix it:
--
--         UPDATE users SET tenant_id = NULL WHERE id = '<row id>';
--
--   (b) Tenant-scoped superadmin seat or service account. Leave as-is.
--
-- No default: picking the wrong disposition either over-grants or loses access.
