-- Row-Level Security for tenant isolation. Apply last, after the other schema files.
-- Each atomic step is its own DO block; CREATE POLICY precedes ENABLE/FORCE so a table
-- never ends up with RLS but no policy.
--   psql -v yuviz_app_password='...' -f database/rls.sql

-- ── Roles and grants ─────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yuviz_platform') THEN
        CREATE ROLE yuviz_platform NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
    END IF;
END $$;

-- \gexec, not a DO block: psql `:'var'` substitution doesn't work inside dollar quotes.
SELECT format(
    'CREATE ROLE yuviz_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'yuviz_app_password')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yuviz_app')
\gexec

-- Re-assert attributes in case the roles pre-existed.
ALTER ROLE yuviz_app      NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
ALTER ROLE yuviz_platform NOSUPERUSER BYPASSRLS   NOCREATEDB NOCREATEROLE NOLOGIN;

GRANT yuviz_platform TO yuviz_app;   -- membership only; BYPASSRLS activates on SET ROLE, never inherited

GRANT USAGE ON SCHEMA public TO yuviz_app, yuviz_platform;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO yuviz_app, yuviz_platform;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO yuviz_app, yuviz_platform;
REVOKE CREATE ON SCHEMA public FROM yuviz_app, yuviz_platform;

-- Future tables created by the schema-applying role get these grants automatically.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO yuviz_app, yuviz_platform;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO yuviz_app, yuviz_platform;

-- ── audit_log backfill ──────────────────────────────────────────────────────
-- Fills tenant_id on old rows. Must run before the audit_log policy; unmatched rows
-- stay NULL (platform-only), which fails closed.
DO $$
DECLARE m RECORD;
BEGIN
    FOR m IN SELECT * FROM (VALUES
        ('agent','agents'), ('campaign','campaigns'), ('carrier','carriers'),
        ('custom_api','custom_apis'), ('invite','user_invites'), ('kb_document','kb_documents'),
        ('knowledge_base','knowledge_bases'), ('phone_number','phone_numbers'),
        ('provider_config','provider_configs'), ('purchased_number','purchased_numbers'),
        ('telephony_config','telephony_configs'), ('tool_provider_config','tool_provider_configs'),
        ('user','users')
    ) AS v(entity_type, tbl) LOOP
        EXECUTE format(
            'UPDATE audit_log a SET tenant_id = t.tenant_id FROM %I t '
            'WHERE t.id = a.entity_id AND a.entity_type = $1 AND a.tenant_id IS NULL', m.tbl)
        USING m.entity_type;
    END LOOP;

    -- Joined through tenants so hard-deleted tenants stay NULL instead of violating the FK.
    UPDATE audit_log a SET tenant_id = t.id
      FROM tenants t
     WHERE a.entity_type = 'tenant' AND a.entity_id = t.id AND a.tenant_id IS NULL;

    -- Three child entities have no tenant column of their own; they inherit via agents.
    UPDATE audit_log a SET tenant_id = ag.tenant_id
      FROM agent_tool_policies p JOIN agents ag ON ag.id = p.agent_id
     WHERE p.id = a.entity_id AND a.entity_type = 'agent_tool_policy' AND a.tenant_id IS NULL;
    -- agent_retrieval_policies is keyed by agent_id.
    UPDATE audit_log a SET tenant_id = ag.tenant_id
      FROM agent_retrieval_policies p JOIN agents ag ON ag.id = p.agent_id
     WHERE p.agent_id = a.entity_id AND a.entity_type = 'agent_retrieval_policy' AND a.tenant_id IS NULL;
    UPDATE audit_log a SET tenant_id = ag.tenant_id
      FROM agent_workflow_versions v JOIN agents ag ON ag.id = v.agent_id
     WHERE v.id = a.entity_id AND a.entity_type = 'agent_workflow' AND a.tenant_id IS NULL;

    -- live_call_interventions.tenant_id is a TEXT slug, so it joins through tenants.slug.
    UPDATE audit_log a SET tenant_id = t.id
      FROM live_call_interventions i JOIN tenants t ON t.slug = i.tenant_id
     WHERE i.id = a.entity_id AND a.entity_type = 'live_call_intervention' AND a.tenant_id IS NULL;
END $$;

-- ── UUID tenant column ──────────────────────────────────────────────────────

DO $$
BEGIN
    DROP POLICY IF EXISTS provider_configs_tenant_isolation ON provider_configs;
    CREATE POLICY provider_configs_tenant_isolation ON provider_configs
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE provider_configs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE provider_configs FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS agents_tenant_isolation ON agents;
    CREATE POLICY agents_tenant_isolation ON agents
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
    ALTER TABLE agents FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS tool_provider_configs_tenant_isolation ON tool_provider_configs;
    CREATE POLICY tool_provider_configs_tenant_isolation ON tool_provider_configs
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE tool_provider_configs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE tool_provider_configs FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    -- NULL tenant_id = platform account, reachable only via platform_conn().
    DROP POLICY IF EXISTS users_tenant_isolation ON users;
    CREATE POLICY users_tenant_isolation ON users
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE users ENABLE ROW LEVEL SECURITY;
    ALTER TABLE users FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    -- user_invites.tenant_id is nullable for the same reason as users.
    DROP POLICY IF EXISTS user_invites_tenant_isolation ON user_invites;
    CREATE POLICY user_invites_tenant_isolation ON user_invites
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE user_invites ENABLE ROW LEVEL SECURITY;
    ALTER TABLE user_invites FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    -- No policy on purpose: only platform_conn() may touch these pre-auth tables.
    ALTER TABLE pending_registrations ENABLE ROW LEVEL SECURITY;
    ALTER TABLE pending_registrations FORCE  ROW LEVEL SECURITY;
    ALTER TABLE email_change_requests ENABLE ROW LEVEL SECURITY;
    ALTER TABLE email_change_requests FORCE  ROW LEVEL SECURITY;
    ALTER TABLE password_reset_requests ENABLE ROW LEVEL SECURITY;
    ALTER TABLE password_reset_requests FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS carriers_tenant_isolation ON carriers;
    CREATE POLICY carriers_tenant_isolation ON carriers
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE carriers ENABLE ROW LEVEL SECURITY;
    ALTER TABLE carriers FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS phone_numbers_tenant_isolation ON phone_numbers;
    CREATE POLICY phone_numbers_tenant_isolation ON phone_numbers
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE phone_numbers ENABLE ROW LEVEL SECURITY;
    ALTER TABLE phone_numbers FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS purchased_numbers_tenant_isolation ON purchased_numbers;
    CREATE POLICY purchased_numbers_tenant_isolation ON purchased_numbers
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE purchased_numbers ENABLE ROW LEVEL SECURITY;
    ALTER TABLE purchased_numbers FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS campaigns_tenant_isolation ON campaigns;
    CREATE POLICY campaigns_tenant_isolation ON campaigns
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE campaigns ENABLE ROW LEVEL SECURITY;
    ALTER TABLE campaigns FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS dnc_numbers_tenant_isolation ON dnc_numbers;
    CREATE POLICY dnc_numbers_tenant_isolation ON dnc_numbers
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE dnc_numbers ENABLE ROW LEVEL SECURITY;
    ALTER TABLE dnc_numbers FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS custom_apis_tenant_isolation ON custom_apis;
    CREATE POLICY custom_apis_tenant_isolation ON custom_apis
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE custom_apis ENABLE ROW LEVEL SECURITY;
    ALTER TABLE custom_apis FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS api_chain_runs_tenant_isolation ON api_chain_runs;
    CREATE POLICY api_chain_runs_tenant_isolation ON api_chain_runs
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE api_chain_runs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE api_chain_runs FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS api_side_effect_claims_tenant_isolation ON api_side_effect_claims;
    CREATE POLICY api_side_effect_claims_tenant_isolation ON api_side_effect_claims
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE api_side_effect_claims ENABLE ROW LEVEL SECURITY;
    ALTER TABLE api_side_effect_claims FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS knowledge_bases_tenant_isolation ON knowledge_bases;
    CREATE POLICY knowledge_bases_tenant_isolation ON knowledge_bases
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE knowledge_bases ENABLE ROW LEVEL SECURITY;
    ALTER TABLE knowledge_bases FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS kb_documents_tenant_isolation ON kb_documents;
    CREATE POLICY kb_documents_tenant_isolation ON kb_documents
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE kb_documents ENABLE ROW LEVEL SECURITY;
    ALTER TABLE kb_documents FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS kb_chunks_tenant_isolation ON kb_chunks;
    CREATE POLICY kb_chunks_tenant_isolation ON kb_chunks
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE kb_chunks ENABLE ROW LEVEL SECURITY;
    ALTER TABLE kb_chunks FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS telephony_configs_tenant_isolation ON telephony_configs;
    CREATE POLICY telephony_configs_tenant_isolation ON telephony_configs
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE telephony_configs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE telephony_configs FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    -- NULL tenant_id = platform mutation with no target tenant. Must follow the backfill.
    DROP POLICY IF EXISTS audit_log_tenant_isolation ON audit_log;
    CREATE POLICY audit_log_tenant_isolation ON audit_log
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
    ALTER TABLE audit_log FORCE  ROW LEVEL SECURITY;
END $$;

-- ── TEXT-slug tenant column ─────────────────────────────────────────────────

DO $$
BEGIN
    DROP POLICY IF EXISTS calls_tenant_isolation ON calls;
    CREATE POLICY calls_tenant_isolation ON calls
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_slug', true), ''))
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_slug', true), ''));
    ALTER TABLE calls ENABLE ROW LEVEL SECURITY;
    ALTER TABLE calls FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS live_call_interventions_tenant_isolation ON live_call_interventions;
    CREATE POLICY live_call_interventions_tenant_isolation ON live_call_interventions
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_slug', true), ''))
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_slug', true), ''));
    ALTER TABLE live_call_interventions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE live_call_interventions FORCE  ROW LEVEL SECURITY;
END $$;

-- ── Child tables scoped via parent join ─────────────────────────────────────
-- The parent's policy applies inside the subquery, so these can't drift from it.

DO $$
BEGIN
    DROP POLICY IF EXISTS agent_tool_policies_tenant_isolation ON agent_tool_policies;
    CREATE POLICY agent_tool_policies_tenant_isolation ON agent_tool_policies
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM agents p WHERE p.id = agent_tool_policies.agent_id))
        WITH CHECK (EXISTS (SELECT 1 FROM agents p WHERE p.id = agent_tool_policies.agent_id));
    ALTER TABLE agent_tool_policies ENABLE ROW LEVEL SECURITY;
    ALTER TABLE agent_tool_policies FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS agent_workflow_versions_tenant_isolation ON agent_workflow_versions;
    CREATE POLICY agent_workflow_versions_tenant_isolation ON agent_workflow_versions
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM agents p WHERE p.id = agent_workflow_versions.agent_id))
        WITH CHECK (EXISTS (SELECT 1 FROM agents p WHERE p.id = agent_workflow_versions.agent_id));
    ALTER TABLE agent_workflow_versions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE agent_workflow_versions FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS campaign_contacts_tenant_isolation ON campaign_contacts;
    CREATE POLICY campaign_contacts_tenant_isolation ON campaign_contacts
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM campaigns p WHERE p.id = campaign_contacts.campaign_id))
        WITH CHECK (EXISTS (SELECT 1 FROM campaigns p WHERE p.id = campaign_contacts.campaign_id));
    ALTER TABLE campaign_contacts ENABLE ROW LEVEL SECURITY;
    ALTER TABLE campaign_contacts FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS custom_api_params_tenant_isolation ON custom_api_params;
    CREATE POLICY custom_api_params_tenant_isolation ON custom_api_params
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM custom_apis p WHERE p.id = custom_api_params.custom_api_id))
        WITH CHECK (EXISTS (SELECT 1 FROM custom_apis p WHERE p.id = custom_api_params.custom_api_id));
    ALTER TABLE custom_api_params ENABLE ROW LEVEL SECURITY;
    ALTER TABLE custom_api_params FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS agent_custom_apis_tenant_isolation ON agent_custom_apis;
    CREATE POLICY agent_custom_apis_tenant_isolation ON agent_custom_apis
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM agents p WHERE p.id = agent_custom_apis.agent_id))
        WITH CHECK (EXISTS (SELECT 1 FROM agents p WHERE p.id = agent_custom_apis.agent_id));
    ALTER TABLE agent_custom_apis ENABLE ROW LEVEL SECURITY;
    ALTER TABLE agent_custom_apis FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS api_chain_steps_tenant_isolation ON api_chain_steps;
    CREATE POLICY api_chain_steps_tenant_isolation ON api_chain_steps
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM api_chain_runs p WHERE p.id = api_chain_steps.run_id))
        WITH CHECK (EXISTS (SELECT 1 FROM api_chain_runs p WHERE p.id = api_chain_steps.run_id));
    ALTER TABLE api_chain_steps ENABLE ROW LEVEL SECURITY;
    ALTER TABLE api_chain_steps FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS transcript_entries_tenant_isolation ON transcript_entries;
    CREATE POLICY transcript_entries_tenant_isolation ON transcript_entries
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM calls p WHERE p.session_id = transcript_entries.session_id))
        WITH CHECK (EXISTS (SELECT 1 FROM calls p WHERE p.session_id = transcript_entries.session_id));
    ALTER TABLE transcript_entries ENABLE ROW LEVEL SECURITY;
    ALTER TABLE transcript_entries FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS agent_knowledge_bases_tenant_isolation ON agent_knowledge_bases;
    CREATE POLICY agent_knowledge_bases_tenant_isolation ON agent_knowledge_bases
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM agents p WHERE p.id = agent_knowledge_bases.agent_id))
        WITH CHECK (EXISTS (SELECT 1 FROM agents p WHERE p.id = agent_knowledge_bases.agent_id));
    ALTER TABLE agent_knowledge_bases ENABLE ROW LEVEL SECURITY;
    ALTER TABLE agent_knowledge_bases FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS agent_retrieval_policies_tenant_isolation ON agent_retrieval_policies;
    CREATE POLICY agent_retrieval_policies_tenant_isolation ON agent_retrieval_policies
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM agents p WHERE p.id = agent_retrieval_policies.agent_id))
        WITH CHECK (EXISTS (SELECT 1 FROM agents p WHERE p.id = agent_retrieval_policies.agent_id));
    ALTER TABLE agent_retrieval_policies ENABLE ROW LEVEL SECURITY;
    ALTER TABLE agent_retrieval_policies FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS kb_ingestion_jobs_tenant_isolation ON kb_ingestion_jobs;
    CREATE POLICY kb_ingestion_jobs_tenant_isolation ON kb_ingestion_jobs
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM kb_documents p WHERE p.id = kb_ingestion_jobs.document_id))
        WITH CHECK (EXISTS (SELECT 1 FROM kb_documents p WHERE p.id = kb_ingestion_jobs.document_id));
    ALTER TABLE kb_ingestion_jobs ENABLE ROW LEVEL SECURITY;
    ALTER TABLE kb_ingestion_jobs FORCE  ROW LEVEL SECURITY;
END $$;

-- ── Call flows (IVR/OBD) ────────────────────────────────────────────────────
DO $$
BEGIN
    DROP POLICY IF EXISTS call_flows_tenant_isolation ON call_flows;
    CREATE POLICY call_flows_tenant_isolation ON call_flows
        FOR ALL TO yuviz_app
        USING      (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid);
    ALTER TABLE call_flows ENABLE ROW LEVEL SECURITY;
    ALTER TABLE call_flows FORCE  ROW LEVEL SECURITY;
END $$;

DO $$
BEGIN
    DROP POLICY IF EXISTS call_flow_versions_tenant_isolation ON call_flow_versions;
    CREATE POLICY call_flow_versions_tenant_isolation ON call_flow_versions
        FOR ALL TO yuviz_app
        USING      (EXISTS (SELECT 1 FROM call_flows p WHERE p.id = call_flow_versions.call_flow_id))
        WITH CHECK (EXISTS (SELECT 1 FROM call_flows p WHERE p.id = call_flow_versions.call_flow_id));
    ALTER TABLE call_flow_versions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE call_flow_versions FORCE  ROW LEVEL SECURITY;
END $$;

-- ── Intentionally without RLS ───────────────────────────────────────────────
-- tenants: it IS the tenant, and tenant_conn()'s own resolver reads it.
-- conversation_node_heartbeats: infrastructure, not tenant-owned.
-- kamailio_cdr: written by Kamailio directly, not by any of these services.
