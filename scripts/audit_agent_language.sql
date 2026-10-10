-- Agents whose behaviour changes when agents.language starts reaching the providers
-- (feature/multilingual-agents). Until now the runtime ignored agents.language; after it,
-- agents.language overrides the STT and TTS provider row's own language, and an explicit
-- agents.language also picks the spoken system strings and the guardrail lexicon.
--
-- Usage (cross-tenant, read-only):
--   psql "$POSTGRES_ADMIN_DSN" -v ON_ERROR_STOP=1 -f scripts/audit_agent_language.sql
-- With the yuviz_app DSN, RLS hides other tenants; the SET ROLE below lifts that for
-- this transaction only.
-- Review step only: every returned row needs a human decision (keep, or NULL it).

BEGIN READ ONLY;
SET LOCAL ROLE yuviz_platform;

SELECT t.slug                              AS tenant,
       a.slug                              AS agent,
       a.status,
       a.language                          AS agent_language,
       stt.engine                          AS stt_engine,
       stt.language                        AS stt_language,
       tts.engine                          AS tts_engine,
       tts.language                        AS tts_language,
       a.language IS DISTINCT FROM stt.language AS stt_changes,
       a.language IS DISTINCT FROM tts.language AS tts_changes,
       a.updated_at
  FROM agents a
  JOIN tenants t ON t.id = a.tenant_id
  -- An agent without its own provider uses the tenant default.
  LEFT JOIN provider_configs stt ON stt.id = COALESCE(a.stt_config_id, t.default_stt_config_id)
  LEFT JOIN provider_configs tts ON tts.id = COALESCE(a.tts_config_id, t.default_tts_config_id)
 WHERE a.deleted_at IS NULL
   AND NULLIF(btrim(a.language), '') IS NOT NULL
   AND (a.language IS DISTINCT FROM stt.language OR a.language IS DISTINCT FROM tts.language)
 ORDER BY t.slug, a.slug;

ROLLBACK;

-- A row is "accidental" when agent_language contradicts what the agent actually speaks
-- (e.g. agent_language = 'en' on an agent whose prompt and voice are Hindi, or a value
-- that isn't a language code). Null those in schema.sql's migration block, by id:
--
--   UPDATE agents SET language = NULL WHERE id IN ('<agent id>', ...);
