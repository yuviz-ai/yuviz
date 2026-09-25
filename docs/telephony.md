# Unified Telephony Service

`services/telephony/` — one FastAPI process that owns every REST-capable
telephony vendor (Vobiz, Cloudonix, and any future `ITelephonyProvider`
registered in `libs/telephony_sdk/`), inbound webhooks and outbound
triggers alike. It replaces `services/vobiz/` and `services/cloudonix/`,
which are decommissioned once the cutover below completes. See
`.sdlc/unified-telephony-service/02-design.md` for the full design and
security rationale; this doc is the operator-facing route table, env vars
and cutover runbook.

The native 5000-5009 DID path (Kamailio -> FreeSWITCH -> C++ Gateway ->
Conversation) is untouched and never enters this process — see the
design's Latency section for the per-path trace.

## Routes

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/{provider}/voice/{account_ref}` | GET, POST | vendor signature over that account's credentials | Inbound call webhook — verifies, rate-limits, resolves the tenant/agent route, and returns the provider's answer XML pointing at the WS stream URL |
| `/{provider}/stream/{token}` | WS | the server-minted admission token in the path | Media stream bridge — claims the one-time token, then runs `MediaStreamBridge` unchanged |
| `/{provider}/status/{account_ref}` | POST | vendor signature over that account's credentials | Hangup/ring callback — records the vendor's call id under the signing account's own tenant, then best-effort notifies Campaigns |
| `/{provider}/call` | POST | `Authorization: Bearer` (console admin/superadmin, or a NULL-tenant service account) | Places an outbound call: `{tenant_slug, agent_slug, to, from, idempotency_key}` |
| `/{provider}/call/idempotency/{key}?tenant_slug=…` | GET | same as above | Read-only replay of a claimed idempotency key — `{"status": "pending"}` while in flight, 404 once expired or if the key belongs to another tenant |
| `/sms/send` | POST | same as above | Sends an SMS via the tenant's default-outbound provider: `{tenant_slug, to, from, text, idempotency_key}` |
| `/health` | GET | none | `{"loaded": <bool>}` — the docker-compose healthcheck target |

Outbound routes never derive a tenant from the request body directly: the
`tenant_slug` field is checked against the caller's own JWT
(`services/telephony/auth.py`), and the caller-id DID / agent are
re-resolved server-side against that tenant before any vendor is called
(`services/telephony/ownership.py`).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PORT` / `TELEPHONY_PORT` | `8750` | Bind port |
| `TELEPHONY_BIND_ADDR` | `127.0.0.1` | Host bind address for the compose port publish — never the shared `BIND_ADDR` |
| `TELEPHONY_PUBLIC_BASE_URL` | *(required)* | Public URL vendors reach this service at — used to build the WS stream URL and outbound answer/hangup/ring callback URLs |
| `CONFIG_SERVICE_URL` | `http://localhost:8000` | Config Service base URL |
| `CONFIG_SERVICE_EMAIL` / `CONFIG_SERVICE_PASSWORD` | — | Service-account credentials for the account preload and agent-ownership repair fetch |
| `SECRET_ENCRYPTION_KEY` | — | Decrypts `enc:` credential fields read from Config Service |
| `CAMPAIGNS_SERVICE_URL` | `http://localhost:8400` | Best-effort outbound-call-resolved notification target |
| `REDIS_URL` | `redis://localhost:6379/0` | Idempotency keys, health status, `did:{did}` reads |
| `TELEPHONY_REFRESH_S` | `300` | Account-store cold-path refresh interval |
| `TELEPHONY_HEALTH_INTERVAL_S` | `300` | Health-probe loop interval |
| `TELEPHONY_ACCOUNT_LIMIT` / `TELEPHONY_DID_LIMIT` | `300` / `30` | Inbound-webhook rate-limit buckets (per 60s window) |

Campaigns' `services/campaigns/telephony_originate.py` additionally reads
`TELEPHONY_SERVICE_URL` (default `http://localhost:8750`).

## Cutover runbook

Three merges against one design — **all three have landed**; the two
per-vendor services this replaced are gone from the repo (this section is
the historical record of how the cutover happened, kept for the next
vendor onboarded the same way):

1. **Build `services/telephony/` and repoint the Vobiz webhook.** Vobiz's
   DID webhook URLs move to `{TELEPHONY_PUBLIC_BASE_URL}/vobiz/voice/{account_ref}`
   (`account_ref` is that tenant's `telephony_configs.id`). Verify with a
   real inbound call, then run `scripts/migrate_telephony_providers.py`
   (see its own docstring) to seal any pre-existing plaintext Vobiz
   `auth_token` — **only after** the webhook is fully repointed, never
   before (a row sealed while the old per-vendor process was still
   reading it live would have broken that still-running process).
2. **Repoint the Cloudonix Voice Application and generalize Campaigns.**
   Update each Voice Application's webhook URL to
   `{TELEPHONY_PUBLIC_BASE_URL}/cloudonix/voice/{account_ref}`. Campaigns'
   worker now dispatches to `telephony_originate.py` for any provider
   registered in `TelephonyProviderRegistry`, ESL otherwise.
3. **Delete the two old per-vendor processes.** Their service
   directories, `services/campaigns/vobiz_originate.py`, their standalone
   doc, and their `docker-compose.yml`/`start_local.sh` entries are all
   removed — a repo-wide search for either old module path or either old
   port number returns nothing.

During the migration window between steps, the old per-vendor processes
and `services/telephony` (port 8750) ran simultaneously — each
`telephony_configs` row's vendor dashboard URL pointed at exactly one host
at a time, so no route was ever double-bound.

## Exposure model

Same posture as the two decommissioned per-vendor services: the compose entry
publishes **only** `${TELEPHONY_BIND_ADDR:-127.0.0.1}:${TELEPHONY_PORT:-8750}:8750`,
never the shared `BIND_ADDR` (which also exposes Postgres, Redis, Ollama
and the unauthenticated conversation gRPC port). Public reachability is a
dedicated TLS-terminating tunnel or ingress pointed at `:8750` only.
