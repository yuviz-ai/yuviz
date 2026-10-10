# Security review: `git diff redesign..HEAD` on feature/one-click-crm-integrations (code mode)
VERDICT: AMBER

No cross-tenant read or write, no privilege escalation, and no IDOR was found in this diff. Every
new query carries an explicit `tenant_id` predicate or runs inside `tenant_conn` RLS. The three
items below are a logging-configuration fragility, an accepted-residual prompt-injection path made
concrete, and a correctness note on the refresh CAS.

## Findings

1. [medium] The one control keeping caller phone numbers out of the logs is bound to one launcher,
   not to the app — `services/toolexec/__main__.py:9-14`
   `configure_logging()` sets `httpx`/`httpcore` to WARNING, and it is called only from
   `__main__.main()`. `services/toolexec/app.py` does nothing equivalent at import or in its
   lifespan. The new CRM lookups put the caller's number in the request *URL*: Zoho
   `?phone=+9198…` (`presets.py:_zoho_steps`) and Salesforce `?q=9198…`
   (`presets.py:_salesforce_steps`), both via `_caller_id(..., location="query")`. Today
   `deployment/docker/docker-compose.yml:153` and `scripts/start_local.sh:124` both use
   `python -m services.toolexec`, so production is safe — but the safety is one line in a launcher,
   not a property of the service.
   Attack: anyone who can read the toolexec log stream (an operator, the log aggregator, a support
   bundle, a misconfigured sidecar) after someone starts the app any other way —
   `uvicorn services.toolexec.app:app`, gunicorn, a k8s manifest, a future `--reload` dev command,
   an embedded test harness — harvests the phone number of every inbound caller across every
   tenant from a single file. No application bug is needed; one deployment change silently turns
   the logging on.
   Note on the existing test: `services/toolexec/tests/test_logging_config.py:28` calls
   `configure_logging()` itself and then asserts URLs are absent. That proves the function works;
   it cannot fail when the deployed path stops calling it (lesson 12).
   Fix: move the two `logging.getLogger(...).setLevel(logging.WARNING)` lines into
   `services/toolexec/app.py` module scope (or its lifespan) so they run however the ASGI app is
   loaded, keep the `__main__` call, and change the tripwire to import `services.toolexec.app` and
   assert `logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING`. Second layer:
   send the lookup number in the request body where the provider allows it, or mark the
   `caller_id` query params `sensitive=True` so it is also redacted in `arguments_redacted`.

2. [low] CRM contact text is not tenant-authored data, and an outsider who can write a lead can
   choose ~100 characters that reach the model's context and the caller's ear —
   `services/toolexec/presets.py:_crm_clean` / `_crm_spoken` / `_crm_contact_projection`
   Attack: an outsider submits a public web-to-lead / contact form on the tenant's own site with
   `firstname` set to up to 100 characters of plain instruction text (letters, digits, spaces and
   `.,'-&/()#` all survive `_crm_clean`) and `phone` set to a number they control. They then call
   the tenant's agent from that number. `phone_suffix_match` matches on the last 9 digits, the
   single survivor's `full_name` is folded verbatim into the LLM tool-result history
   (`services/conversation/tools/orchestrator.py` fold path, asserted in
   `services/conversation/tests/test_execute_api_tool.py`), and `_crm_spoken` places the same text
   inside `deterministic_response`, which the orchestrator speaks verbatim
   (`services/conversation/tools/orchestrator.py:164`). The attacker gets bounded influence over
   one model turn on their own call.
   This is the residual the design accepted, but the design called CRM content "tenant data"; it
   is partly outsider-written. Containment holds: `{ } " \` and every control character are
   dropped, the carrier sentence "Caller matched:" prefixes it, and the 100/120 caps bound it — so
   nothing can terminate the JSON string in `items` or open/close a `{{placeholder}}`.
   Fix: nothing is required for isolation. If tightened, drop `Nd` digits from the *spoken* path
   (a dictated phone number is the highest-value payload at 100 chars) and leave `items` as is.

3. [low] Concurrent refresh can let both racers win, so the stored access token is not necessarily
   the one the winner returned — `services/toolexec/oauth.py:528-536`
   The CAS predicate is `refresh_token_ref = $3`. When the provider returns no new refresh token,
   `refresh_token_ref = COALESCE($6, refresh_token_ref)` leaves the column unchanged, so a second
   concurrent refresh still satisfies the same predicate and also commits.
   Attack: there is no attacker — this is a correctness and availability issue, and it is recorded
   as a finding only because it is a visible tenant-facing failure. Against a provider that
   invalidates the previous access token on each refresh (Salesforce under a restrictive session
   policy), two concurrent calls leave the row holding one token while the other caller uses a
   token the provider has already killed, producing an intermittent spurious `reconnect_required`
   for that tenant's connector. No secret is leaked: the loser returns its own in-memory token and
   never reads the winner's ref.
   Fix: add `AND access_token_ref = $7` (the ref read at the top of `access_token_for`) to the CAS
   predicate so the token the racer actually started from, not just the refresh ref, is the
   precondition.

## Verified controls

- **Projection is closed on every path.** `_crm_contact_projection` (presets.py) returns only
  `_crm_result(...)` → exactly `{outcome, items, spoken}`; there is no other return. Non-match and
  ambiguous both return `items=[]`, `spoken=""`. `items` entries are built by one dict literal with
  exactly `contact_id, full_name, company, owner_name`. `match_count` appears nowhere in the CRM
  path (only in the pre-existing `google_booking_lookup`, presets.py:666/679).
- **The transform is total and never raises.** `apply_response_transform` dispatches
  `crm_contact_projection` *before* the `isinstance(response, dict)` guard (presets.py:461-463), and
  `_crm_contact_projection` guards `isinstance(response, dict)`, `"_raw" not in response`,
  `isinstance(records, list)` and `all(isinstance(r, dict))` — a list body, a string body, a
  truncated body or a provider error page all land on `no_match`, not an exception.
- **Nothing downstream can add a key.** `executor.py:877-890` replaces `raw_response` with the
  projection *before* `prior_responses[api_id] =`, before `redaction.redact`, before `_persist_step`
  and before `data = final_redacted_response`. The only other consumer is
  `_interpolate_success_template` over the same dict.
- **Step-3a containment lives inside the transform, not at callers.** `_crm_clean` runs per field
  inside `_crm_contact_projection`; the codepoint allowlist is `category[0] in "LM"`, `Nd`, `Zs`→
  space, plus `_CRM_KEPT_PUNCTUATION = ".,'-&/()#"`. `{`(Ps), `}`(Pe), `"`(Po), `\`(Po) and all
  `Cc`/`Cf` are dropped, so no value can terminate the JSON string in `items` nor open or close a
  `{{…}}` placeholder that `_TEMPLATE_PLACEHOLDER_RE` (`executor.py:648`) would see. Filter runs
  before the `[:_CRM_FIELD_CAP]` slice, so truncation cannot reveal a payload. `_crm_spoken` stops
  before the first field that would exceed `_CRM_SPOKEN_CAP=120`, which equals the per-placeholder
  truncation in `_interpolate_success_template` (`text[:120]`), so no mid-word cut.
- **`access_token_for`'s two call sites have not drifted.** `auth_schemes.py:226-230` uses the
  per-row `oauth.provider_host_allowed(provider, urlsplit(effective_url).hostname, api_base_url,
  base_source=api["endpoint_base_source"])`; `oauth.post_json` (oauth.py:451) still uses the literal
  `urlsplit(url).hostname not in provider.api_hosts`, so a tenant-influenced `instance_url` cannot
  widen the fixed-host setup call. `provider_host_allowed` (oauth.py:424-431) is exact-host
  equality against the stored base when `base_source == "oauth_connection"` and deliberately does
  not consult `api_host_suffixes`.
- **The host check is judged on the URL actually dialed.** `executor.py:786` validates `url` (the
  composed, path-substituted URL), and `executor.py:789` passes `effective_url=url` to
  `auth_schemes.apply`. This is the bug class the design mis-stated; the implementation is correct.
- **`api_base_url` cannot be pointed at an internal host.** Three layers, all present:
  the DB CHECK `^https://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$` (origin only, no path/port/
  userinfo, schema.sql); `_validated_api_base` (oauth.py:201-213) requiring
  `claim == f"https://{host}"`, `parts.port is None` and `host` under
  `api_host_suffixes` (`.my.salesforce.com`/`.salesforce.com`, `.zohoapis.<tld>`); and
  `resolve_and_validate_endpoint(api_base_url)` at connect time plus
  `resolve_and_validate_endpoint(url)` on the composed URL at every call, which denies private/
  link-local/loopback resolutions and pins the result through `PinnedResolverTransport`. The value
  itself is never tenant-supplied — it comes only from the provider's token response over a
  fixed `token_url` (Zoho's `accounts_server` is constrained to `spec.accounts_servers`,
  oauth.py:302-303).
- **Path composition cannot escape the origin.** `endpoint_base_source='oauth_connection'` requires
  `endpoint_url ~ '^/'` and a non-NULL `oauth_connection_id` (`custom_apis_endpoint_base_shape`);
  path params are `quote(..., safe="")` with a `".." in string_value` rejection
  (`executor.py:437-441`); and preset-created rows are immutable — `custom_apis.py:601` raises
  `preset_managed` before any lock or write, so a tenant admin cannot rewrite the path.
  `CustomApiCreate`/`CustomApiUpdate` now carry `extra="forbid"` (schemas.py:32/51) and expose
  neither `endpoint_base_source` nor the other preset-only columns.
- **`revoke_style="path"`.** `oauth.py:609-611`: one `DELETE` via `_provider_call`, no retry loop,
  `quote(refresh_token, safe="")`, and the `except` logs only `connection_id`
  (`oauth.py:618-619`) — no URL and no exception text reach the log. `revoke_style="none"`
  short-circuits cal.com at `oauth.py:591`.
- **Tenant scoping on every new query.** `connection_api_base` (`WHERE tenant_id = $1 AND id = $2
  AND deleted_at IS NULL AND status = 'connected'`), `access_token_for` (`WHERE tenant_id = $1 AND
  id = $2`), `get_connected` (`WHERE tenant_id = $1 AND provider = $2`), `_winners_token`, both
  `_refresh` CAS updates, `disconnect`'s conditional update, and `_upsert_connection`'s
  `ON CONFLICT (tenant_id, provider)` — all explicit, all inside `tenant_conn`.
- **`TOOLEXEC_CALCOM_ENABLED` gates both surfaces.** `configured_providers()` (oauth.py:166-176)
  is the single decision point and is consulted by the provider listing
  (`routers/oauth_connections.py:41`), `start_authorization` (:230), `complete_authorization`
  (:299) and `connect_api_key` (:387). `start_authorization` additionally refuses
  `auth_kind != "oauth2"` and `connect_api_key` refuses `auth_kind != "api_key"`, so neither flow
  can overwrite the other's connection row through the shared `_upsert_connection`.
- **`connect_api_key` is authorized and does not leak the key.** `require_role("superadmin",
  "admin")` plus `await assert_tenant_access(tenant_id, current_user)`
  (`routers/oauth_connections.py:82-85`); the key arrives as `SecretStr` in a JSON body (not a URL
  — `admin-ui/lib/toolexecApi.ts` posts it in `body`); it is sealed with `encrypt_tenant_secret`
  only, never through a resolver that would accept `env:`/`k8s:` pointers; the verify call's
  failure is re-raised as a bare `ValueError("oauth_connection_failed") from None` and surfaces as
  a constant 400; and the returned row is `_PUBLIC_COLUMNS` (no `*_ref`, no `provider_sub`).
  Nothing is stored when verification fails.
- **The token-shape CHECK is fail-closed for the new kind.** `oauth_connections_token_shape` forces
  `api_key` rows to have `refresh_token_ref IS NULL AND access_expires_at IS NULL`, which is
  exactly the branch `access_token_for` takes without touching `_refresh`. Both the
  `oauth_connections` and the `oauth_authorization_states` provider CHECKs were widened, in one
  `DO $$` block with their own DROPs (lessons 10/13).
- **SOSL/search injection is not reachable.** `_caller_id(..., digits_only=True)` uses
  `remote_party.lstrip("+")`, which looks weaker than "digits only" — but `remote_party` can only
  be `normalize_ani`'s output (`presets.py:408-413`), i.e. `"+"` followed by 8–15 digits and
  nothing else, and `_resolve_arguments` reads it from `remote_party` alone, never from
  `caller_arguments`. So the Salesforce `q` and Zoho `phone` values are pure digits/E.164.
- **The shared-grant `platform_conn` registration matches the code.** `tests/test_rls_coverage.py`
  reason `"oauth-shared-grant-check"` describes `_revoke_upstream` (oauth.py:594-607) accurately:
  `$2` is the disconnecting row's own `provider_sub` returned by `disconnect`'s UPDATE, never a
  request field; the only output is a boolean consumed in-process; and `disconnect` returns a
  constant `{"disconnected": True}` from the foreground path, with the probe and revoke deferred
  to `background_tasks`, so neither body nor latency varies (lesson 2).
