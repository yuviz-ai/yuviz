# Setup — Web-Testing Path (Fork & Run)

> **Most people should run `./deployment/sh/dev.sh` instead** — one command, Docker the only
> prerequisite, works on macOS/Linux/Windows (PowerShell, Command Prompt,
> Git Bash or WSL2). See the
> Quickstart in [README.md](../README.md). This document covers the **native**
> install, which is useful for debugging a service directly against your own
> Python environment but has more moving parts and was only verified on macOS.

Status: **built and verified**. Covers the subset of this
platform that a fresh fork can actually run: the Config Service, Knowledge
Service, Conversation Service (STT/LLM/TTS pipeline), the webcall bridge,
and admin-ui's "Test Agent" browser panel. This deliberately excludes
native SIP/telephony (Kamailio, FreeSWITCH, the C++ Gateway, MySQL) — that
infra lives outside this repo (system packages + unversioned local config)
and isn't reproducible by a fork; see `scripts/start_local.sh` if you have
that stack already installed. Everything below was verified end-to-end
against a genuinely fresh venv and a fresh database on macOS, not just the
long-lived dev environment — but only on macOS. This path has been
audited for macOS-only code (none found in the required default
providers — `faster_whisper`/`ollama`/`kokoro` are all cross-platform,
and the one macOS-only TTS provider is opt-in, not on by default) but has
not actually been run on Linux. If you hit a Linux-specific issue,
please report it rather than assuming it's expected.

## 1. Prerequisites

- **PostgreSQL** (14+) and **Redis** — the only required data stores.
- **Python 3.11** — this codebase has only ever run on 3.11; nothing here
  has been tested on 3.12+.
- **Node.js** — for admin-ui (Next.js).
- **Ollama** — only if you want fully local STT/LLM/TTS with no API keys
  (the seed script's default: `faster_whisper` + `llama3.2` + `kokoro`). If
  using Ollama, also pull the model before starting the stack — the seed
  script only registers `llama3.2` as the tenant's default, it doesn't
  download it: `ollama pull llama3.2`. Skip both if you'd rather wire in a
  cloud provider (Deepgram, Gemini,
  ElevenLabs — see `services/conversation/providers/`) through the admin
  UI after setup.

On macOS with Homebrew: `brew install postgresql@14 redis node ollama`.

## 2. Python environment

```bash
cd voice-ai-platform
python3.11 -m venv venv
./venv/bin/pip install -r requirements.txt
```

`requirements.txt` is a `pip freeze`-generated, exact-pinned list verified
clean in three separate from-scratch venvs — see its header comment for
the two packages it deliberately pins past their latest-yanked versions
(`grpcio`/`grpcio-tools`/`grpcio-health-checking`, `charset-normalizer`).

## 3. VAD model

`libs/vad_sdk/silero_vad.py` loads `models/silero_vad.onnx`, which isn't
committed to the repo (binary model weights). It's confirmed identical to
the copy `faster-whisper` already bundles as a pip dependency, so just
copy it out instead of downloading anything separately:

```bash
mkdir -p models
cp venv/lib/python3.11/site-packages/faster_whisper/assets/silero_vad_v6.onnx models/silero_vad.onnx
```

## 4. Database

```bash
brew services start postgresql@14
brew services start redis
createdb voiceai
psql voiceai -v ON_ERROR_STOP=1 -f database/schema.sql
psql voiceai -f database/knowledge_schema.sql
psql voiceai -f database/telephony_schema.sql
```

`schema.sql` seeds a `default` tenant row — the seed script in step 6
depends on it existing.

### Environment variables — one `.env`

Every setting lives in the repo's `.env` (gitignored). `.env.example` lists
all of them, grouped and commented. Both launchers, `scripts/start_web_test.sh`
(this guide) and `scripts/start_local.sh` (full telephony stack), load `.env`
when sourced:

- On first run it copies `.env.example` to `.env`, and it adds any key added
  to the example later.
- It generates the blank platform secrets: `JWT_SECRET`,
  `SECRET_ENCRYPTION_KEY`, `CONFIG_SERVICE_PASSWORD` and `YUVIZ_APP_PASSWORD`.
  It never changes a value you already have.
- Each `start_*` block refuses to start, naming the key, if a setting it
  needs is blank.

A variable already set in your shell wins over `.env`, so you can override
one value for a single run. Docker uses `deployment/.env` instead
(`deployment/sh/dev.sh` generates it), because its hosts are container names.

Fill these yourself; nothing can generate them:

| Key | What |
|---|---|
| `FREESWITCH_ESL_PASSWORD` | Must match `password` in FreeSWITCH's `event_socket.conf.xml`. The Gateway and Campaigns refuse to use ESL without it. |
| `KAMAILIO_DB_URL` | Kamailio's MySQL, e.g. `mysql://kamailio:<password>@localhost/kamailio`. Used by `scripts/update_kamailio_ip.sh`. |
| `TELEPHONY_PUBLIC_BASE_URL` | Public tunnel URL for Vobiz/Cloudonix callbacks. |
| `SMTP_*`, `GOOGLE_*`, provider API keys | Optional features. |

`SIP_IP` (`127.0.0.1` or `auto`) sets where SIP listens; see [telephony-local-setup.md](telephony-local-setup.md#using-your-lan-ip-instead). `SIP_PROXY_HOST` ships blank: `update_kamailio_ip.sh` writes it. Until then the Gateway and Campaigns refuse to dial numbers. To restart them by hand after it changes, use a new terminal tab (or `unset SIP_PROXY_HOST` and source `start_local.sh` again): `_load_env` never replaces a value the shell already exports, so an older tab keeps dialing the old host. `start_gateway` and `start_campaigns_service` warn when the shell and `.env` disagree.

## 5. Service-account credentials

The Conversation Service authenticates to Config as a real user. Create it
with the password the launcher generated into `.env` (sourcing it creates
`.env` on first run and loads it). Run it in a `( ... )` subshell: the
launcher turns on `set -euo pipefail`, and this one-off command fails on a
re-run (the account already exists), which would otherwise close your
terminal:

```bash
( source scripts/start_web_test.sh &&
  ./venv/bin/python3 scripts/create_service_account.py "$CONFIG_SERVICE_EMAIL" "$CONFIG_SERVICE_PASSWORD" )
```

`SECRET_ENCRYPTION_KEY` encrypts provider credentials pasted into the Admin
UI (`enc:` scheme, `libs/config_sdk/secrets.py`). Keep it: losing it makes
every stored `enc:` credential permanently undecryptable. `JWT_SECRET` signs
login tokens and is shared by every service through `.env`.

Invite emails (user onboarding) are sent via stdlib `smtplib` — no new
dependency, no queue. `SMTP_PASSWORD_REF` follows the same `env:`/`k8s:`
secret-ref convention as `api_key_ref`/`auth_token_ref` above; the rest are
plain config, not secrets:

Set them in `.env`:

```bash
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=invites@example.com
SMTP_FROM=invites@example.com
SMTP_PASSWORD_REF=env:SMTP_PASSWORD
SMTP_PASSWORD=abcd efgh ijkl mnop
SMTP_STARTTLS=true
INVITE_BASE_URL=http://localhost:3000
```

Values are read literally, up to the end of the line: no quotes, and spaces
(as in a Gmail app password) are fine.

`INVITE_BASE_URL` is the Admin UI origin the accept link points at —
`${INVITE_BASE_URL}/invite#<token>`, token in the fragment so it never
reaches a server log. An SMTP send failure doesn't fail the invite
request: the invite is still created `pending` and the response reports
`email_sent: false`; resend tries again.

`SMTP_USER` is optional — leave it unset for a relay that needs no
authentication (a local dev sink, or an internal relay that authorises by
source IP); `SMTP_PASSWORD_REF` is only required when `SMTP_USER` is set.

`SMTP_STARTTLS` defaults to `true` and upgrades the connection with
STARTTLS before authenticating — both `SMTP_PASSWORD` and the invite token
itself are bearer-equivalent, so sending either over a cleartext
connection on port 587 is a real credential leak, not just a lint issue.
It must stay `true` everywhere except local development. Set it to
`false` only against a local dev relay with no TLS support at all (e.g.
MailHog, `aiosmtpd` on port 1025) — a relay that refuses STARTTLS is
treated as a send failure (same `email_sent: false` path above), never a
silent fallback to cleartext. The upgrade verifies the relay's certificate
and hostname (no way to disable that while `SMTP_STARTTLS=true`), so the
relay needs a valid, trusted certificate — a self-signed or expired one
will fail the send the same way a refused STARTTLS does.

### Tool Execution Service (custom API chains, port 8600)

`services/toolexec/` (started by `scripts/start_local.sh`'s
`start_toolexec_service()`) owns the custom-API registry and runs
`execute_api` chains on the Conversation Service's behalf. No new
service-account is needed: `services/conversation/tools/providers/toolexec/
client.py` (`ToolExecClient`) authenticates to Config Service's
`/auth/login` with the SAME `conversation-service@internal.yuviz.ai`
account created in step 5 above, reusing `CONFIG_SERVICE_EMAIL`/
`CONFIG_SERVICE_PASSWORD`/`CONFIG_SERVICE_URL` — nothing new to create
there. Its own env vars:

```bash
export TOOLEXEC_SERVICE_URL="http://localhost:8600"        # Conversation Service's base URL for it; default shown
export TOOLEXEC_EXECUTE_SUBJECTS="conversation-service@internal.yuviz.ai"  # comma-separated emails allowed to POST /internal/chains/execute; default shown
export TOOLEXEC_TENANT_SECRET_ROOT="/path/to/tenant/secrets"  # where tenant-namespaced enc:/env:/k8s: refs resolve — NEVER the platform k8s secret mount; required, no default
export TOOLEXEC_HTTP_HOST_ALLOWLIST=""                      # comma-separated hostnames allowed to use http:// or a non-default port instead of https://; empty = https-only, no exceptions
export TOOLEXEC_ARGS_HMAC_KEY_REF="env:TOOLEXEC_HMAC_KEY"   # platform secret ref (CompositeSecretResolver) — required, no default; see rotation note below
export TOOLEXEC_ARGS_HMAC_KEY_ID="k1"                       # key id embedded in every derived hash/idempotency-key, so a rotation is visible in stored data; default shown
export TOOLEXEC_SIDE_EFFECT_CLAIM_TTL="24 hours"            # how long a claimed side-effecting step blocks a retry of the same arguments; default shown
export TOOLEXEC_MAX_CHAIN_BUDGET_MS="30000"                 # hard ceiling on any chain's whole-chain wall clock, regardless of what the caller/agent policy requests; default shown
export TOOLEXEC_MAX_CONCURRENT_RUNS_PER_AGENT="4"           # abuse brake, per (tenant_id, agent_id), per service replica; default shown
export TOOLEXEC_MAX_RUNS_PER_MINUTE_PER_AGENT="60"          # same scope as above; default shown
export TOOLEXEC_MAX_RESPONSE_BYTES="1048576"                # a step's upstream response is abandoned past this many bytes (1 MiB); default shown
```

`TOOLEXEC_ARGS_HMAC_KEY_REF` rotation: `_derive()` (`services/toolexec/
executor.py`) uses this one key, with a different domain-separation tag,
for BOTH the platform's own side-effect claim (`arguments_hash`, stored in
`api_chain_steps`/`api_side_effect_claims`) AND the value sent to the
downstream API in its own idempotency header
(`custom_apis.idempotency_header`). Rotating the key therefore blinds
**both** of those at once — the platform's fail-closed retry guard and the
downstream API's own dedupe — for one `TOOLEXEC_SIDE_EFFECT_CLAIM_TTL`
window, since both are derived from the same rotated secret. Plan a
rotation around that window, not around the platform claim alone.

#### OAuth connectors (Google, Zoho, Microsoft)

Tenants connect their own Google, Zoho or Microsoft account from the console's
**Integrations** page. The platform registers one OAuth app per provider, once,
and supplies it through env vars. A provider with any of its three values unset
is left out of `GET /oauth-providers`, so the console shows no Connect button
for it; with `TOOLEXEC_OAUTH_REDIRECT_URI` unset no provider is offered at all.

```bash
# The console origin plus /integrations/callback. Sent to the provider as the
# redirect URI on every authorize and token call; it is read from here, never
# from a request. It must match, character for character, what you register
# with each provider below.
export TOOLEXEC_OAUTH_REDIRECT_URI="https://console.example.com/integrations/callback"

export TOOLEXEC_OAUTH_GOOGLE_CLIENT_ID="..."
export TOOLEXEC_OAUTH_GOOGLE_CLIENT_SECRET_REF="env:TOOLEXEC_OAUTH_GOOGLE_SECRET"
export TOOLEXEC_OAUTH_ZOHO_CLIENT_ID="..."
export TOOLEXEC_OAUTH_ZOHO_CLIENT_SECRET_REF="env:TOOLEXEC_OAUTH_ZOHO_SECRET"
export TOOLEXEC_OAUTH_MICROSOFT_CLIENT_ID="..."
export TOOLEXEC_OAUTH_MICROSOFT_CLIENT_SECRET_REF="env:TOOLEXEC_OAUTH_MICROSOFT_SECRET"
```

`*_CLIENT_SECRET_REF` is a platform secret ref (`env:` or `k8s:`), resolved by
the platform resolver; the secret itself never goes in the variable. The
console is the origin that receives the browser, so the redirect URI points at
the admin UI (port 3000 in dev: `http://localhost:3000/integrations/callback`),
not at the toolexec service.

Per provider:

- **Google.** Google Cloud console, APIs & Services, Credentials: create an
  OAuth client of type *Web application* and add the redirect URI above under
  *Authorized redirect URIs*. Enable the Google Calendar API and the Google
  Sheets API for the project. On the consent screen add the scopes
  `calendar.events`, `calendar.freebusy` and `drive.file`. The calendar scopes
  are "sensitive", and Google's app verification can take weeks; until it
  clears, only listed test users (up to 100) can consent. Start verification
  when you register the app. Sheets lead capture uses only `drive.file`, which
  is not sensitive.
- **Zoho.** Zoho API console: register a *Server-based Application* and add the
  redirect URI. Enable **multi-DC** on it: a tenant on `.in`, `.eu` and the
  other regional data centres authorizes and is exchanged against its own
  accounts host, and the console passes that host back as `accounts-server`.
  The service accepts only the fixed list of Zoho accounts hosts.
- **Microsoft.** Entra (Azure AD) app registration, supported account type
  *any organizational directory and personal accounts*, platform *Web*, with the
  redirect URI above, plus a client secret. Microsoft has no refresh-token
  revoke endpoint, so a disconnect only clears Yuviz's copy.

A disconnect always returns `{"disconnected": true}` and revokes upstream after
the response is sent. The console therefore tells the admin to also remove Yuviz
from the connected apps in the provider account.

#### CRM contact lookup and cal.com (Salesforce, HubSpot, Zoho CRM, Dynamics, cal.com)

The CRM presets give an agent a read-only "look up the caller in the CRM"
tool. They reuse the OAuth mechanism above with the same redirect URI, so
nothing about the registrations in the previous section moves. Each provider
follows the same env pattern, and is hidden from the console until all three
values are set:

```bash
export TOOLEXEC_OAUTH_SALESFORCE_CLIENT_ID="..."
export TOOLEXEC_OAUTH_SALESFORCE_CLIENT_SECRET_REF="env:TOOLEXEC_OAUTH_SALESFORCE_SECRET"
export TOOLEXEC_OAUTH_HUBSPOT_CLIENT_ID="..."
export TOOLEXEC_OAUTH_HUBSPOT_CLIENT_SECRET_REF="env:TOOLEXEC_OAUTH_HUBSPOT_SECRET"
# Optional: any scope ticked as required on the HubSpot app beyond the two below.
export TOOLEXEC_OAUTH_HUBSPOT_REQUIRED_SCOPES=""
# Zoho CRM uses the Zoho app registered above: no new variables. The CRM
# preset asks the tenant for one extra scope, ZohoCRM.modules.contacts.READ.
```

- **Salesforce.** Setup, App Manager, *New Connected App*, with *Enable OAuth
  Settings* on, the callback URL set to the redirect URI above, and the OAuth
  scopes `api` and `refresh_token` (offline access). The service sends PKCE.
  `api` grants write as well as read, and Salesforce has no read-only API
  scope, so the read-only guarantee has to come from the org: set the app to
  *Admin approved users are pre-authorized* and assign it only to a read-only
  profile or permission set. Tenants connect against their own org; the
  service follows the `instance_url` Salesforce returns and sends the token
  only to Salesforce hosts.
- **HubSpot.** developers.hubspot.com, create a *public app* (not a private
  app), add the redirect URI above, and select the scopes `oauth` and
  `crm.objects.contacts.read`. HubSpot treats every scope ticked on the app as
  required and shows an error on its consent page if the install URL omits
  one, so tick only those two, or list the extras, space-separated, in
  `TOOLEXEC_OAUTH_HUBSPOT_REQUIRED_SCOPES`. HubSpot does not support PKCE and
  the service does not send it. A tenant installs through the *Connect* button, which sends
  them to the install URL on `app.hubspot.com`; there is no URL to hand out
  yourself.
- **Zoho CRM.** Reuses the Zoho server-based application and its multi-DC
  setting from above. Nothing to register. A tenant whose existing Zoho grant
  lacks the CRM scope must reconnect Zoho to grant it.
- **Dynamics 365.** Not launched. There is no preset and no env var for it, and
  the console shows it as "Not available". It uses the Microsoft app above once
  it is built.
- **cal.com.** Not launched. cal.com has no consent redirect: the tenant admin
  pastes an API key on the Integrations page. It is off unless
  `TOOLEXEC_CALCOM_ENABLED=1` is set on toolexec; unset, `GET /oauth-providers`
  omits it, the console shows "Not available", and the api-key route refuses
  the connection. A cal.com key is account-wide and cannot be scoped, which the
  card says before the paste field. **Verify before setting the flag:** the
  service checks a pasted key with `GET https://api.cal.com/v2/me` and an
  `Authorization: Bearer <key>` header. Neither has been confirmed against
  cal.com's live documentation, and this build could not reach it. Confirm both
  (and that the response carries `data.email`) before enabling it. Revoke
  cal.com keys in cal.com: Yuviz has no upstream revoke for them.

What the operator should tell tenants:

- **Phone matching.** The lookup sends the caller's number as E.164 and as bare
  digits. Zoho and HubSpot compare against the stored phone field exactly, so a
  contact saved in national format (`098765 43210`, `(415) 555-0100`) is not
  found. Salesforce searches on digits only; its matching of formatted numbers has not been checked. A miss is spoken as "no
  match", not as an error.
- **Caller identity is unverified.** The number comes from the carrier's caller
  ID (ANI), which a caller can spoof. A call from a spoofed number matching a
  contact receives that contact's four projected fields (contact id, name, company, account owner). The service returns no other CRM field, but the disclosure is accepted,
  not prevented; do not point the tool at a CRM whose contact names are sensitive.
- **API spend is not throttled.** Every call that reaches the agent can cost a
  CRM API request against the customer's daily limit.

#### Credential refs: who may use `env:` and `k8s:`

`env:` and `k8s:` pointer refs are platform-operator-only input. A tenant admin
who types one into a credential field gets a 400 and pastes the plaintext key
instead; the server encrypts it, bound to that tenant, and the console shows it
back only as `[stored]`. A platform operator keeps the pointer form (for example
`env:PLIVO_AUTH_TOKEN` on a BYOC carrier). Existing rows are untouched by this
rule; only new writes are checked.

A **quarantined** credential reads as an empty, required field in the console
(its stored value is the literal `quarantined`, which no resolver accepts) and
the integration using it fails until the tenant enters the key again.

#### Release step: `reencrypt_tenant_refs.py`

Run this straight after the toolexec rollout. Before it runs, every legacy
`enc:` credential on a custom API fails closed with `credential_unavailable`
(it is never decrypted), so the gap between rollout and script is an outage for
those APIs. It runs under the superuser DSN (`POSTGRES_DSN`) and needs
`SECRET_ENCRYPTION_KEY`, the same key the services use.

```bash
# 1. Dry run: does everything, then rolls back. Prints counts and exits 2 if the ceiling would trip.
POSTGRES_DSN=... SECRET_ENCRYPTION_KEY=... ./venv/bin/python -m services.toolexec.reencrypt_tenant_refs --dry-run

# 2. The real run, with a report for the per-tenant rotation notices.
POSTGRES_DSN=... SECRET_ENCRYPTION_KEY=... ./venv/bin/python -m services.toolexec.reencrypt_tenant_refs --report /secure/path/quarantine.jsonl
```

What it does and what to expect:

- A ciphertext held by exactly one tenant is **rebound** (decrypted once, sealed
  to that tenant). A ciphertext that appears under **two or more tenants** is
  **quarantined in every occurrence, the original holder included**: nothing in
  the database says who copied from whom, so any guess would leave a live
  cross-tenant credential behind. Every tenant that held one must re-enter that
  key. Tell them to rotate it at the provider too, since a second tenant could
  read it.
- `--report <path>` writes one JSON object per quarantined row (JSON Lines), mode
  `0600`, after the transaction commits. It holds ids only: no ciphertext, no
  plaintext, no field value. Keep it with the release records. A dry run writes no
  report. stdout is counts only.
- Exit codes: **0** only when everything resolved (the
  `custom_apis_auth_config_enc_bound` constraint is validated and no shared
  ciphertext is left live); **1** when the run failed and rolled back, or finished
  leaving legacy refs or shared ciphertexts behind; **2** when the blast-radius
  ceiling stopped it before any write. Treat any non-zero exit as "not done" and do
  not continue the rollout.
- The whole run is one transaction, so a partial quarantine cannot happen.
- **Blast-radius ceiling.** Quarantine is destructive and its inputs can be
  planted: a viewer in one tenant can paste copies of that tenant's own live
  ciphertext to get its credentials quarantined, and a wrong
  `SECRET_ENCRYPTION_KEY` makes every ciphertext look undecryptable. The run
  aborts with exit 2, writing nothing, if it would quarantine more than 25 rows
  (`--max-quarantine`), more than 10% of all ref-bearing rows
  (`--max-quarantine-pct`), more than 3 undecryptable ciphertexts, or any
  platform-level `provider_configs` row.
- **`--allow-large-quarantine`** proceeds past the ceiling. It is appropriate
  when you have read the dry-run counts and can account for them: a large,
  known multi-tenant install whose shared ciphertexts are real, with the
  expected key. It is **not** appropriate to clear a failed run just to get a
  zero exit. Exit 2 on a deployment you expected to be clean means find out why
  first. In particular, do not override when the undecryptable count tripped:
  that is the wrong-key signature, so check `SECRET_ENCRYPTION_KEY` instead.
- **Restart after the script.** The quarantine takes effect in the database at
  commit, but long-lived in-process caches keep the old credential until the
  process restarts: `DidProviderManager` (`services/did/provider_manager.py`),
  Conversation's `AIProviderManager` and Knowledge's `EmbeddingProviderManager`
  each hold built provider instances in a per-process `_instances` dict.
  **Restart the DID, Conversation and Knowledge services after a real run**;
  until then a quarantined credential can keep working in a process that
  loaded it earlier.

#### `scripts/seed_default_config.py` and credentials

The seed script's `main` creates and updates the default tenant's provider
configs and agent through the Config service's own functions, passing
`allow_pointer_schemes=False` on both calls. It supplies no key and no ref, so
it needs no pointer scheme, and it is safe to re-run after the release step.
It does not set credentials: add each provider's API key afterwards, as the
tenant would, through the console (the plaintext key, never an `env:` ref).

## 6. Seed a default agent

```bash
export REDIS_URL="redis://localhost:6379/0"
./venv/bin/python3 scripts/seed_default_config.py
```

Idempotent — safe to re-run. Creates `stt/faster_whisper`,
`llm/ollama`, and `tts/kokoro` provider_configs plus a `default` agent
under the `default` tenant, matching `config/agents/default.yaml`'s
content so a local test call sounds the same as before this config moved
into the database.

## 7. Start the stack

```bash
source scripts/start_web_test.sh
```

This loads shell functions rather than starting everything at once — run
each in its own terminal tab, in this order:

1. `start_data` (if you skipped step 4 above manually)
2. `start_ollama` (only if using local models)
3. `start_config_service`
4. `start_knowledge_service` (optional — only needed for RAG-backed agents)
5. `start_conv1`
6. `start_webcall`
7. `start_admin_ui`

Then, from a fresh tab, run `./scripts/verify_setup.sh` — unlike the
`verify` shell function (a quick port-only check), this confirms each
service actually reports healthy: it calls Config Service's and
Knowledge Service's `/health` endpoints and makes a real gRPC health
check against Conversation Service, not just a TCP connect.

`start_web_test.sh` deliberately does not start Tool Execution Service —
only `scripts/start_local.sh` (the full native stack, see §9) does, via
its own `start_toolexec_service()`. Add it to your tab order between
`start_knowledge_service` and `start_conv1` if you're testing `execute_api`
chains locally; `start_local.sh`'s own `verify`/`portmap` already cover
its `:8600` port and `/health` check.

## 8. Test an agent

Open `http://localhost:3000`. On a database that has no superadmin yet the
login page opens on **Create your administrator account** — enter your own
email and a password of at least 8 characters (minimum enforced by
`services/config/schemas.py`), and you are signed in as the first superadmin.
The form reverts to plain sign-in for good once that account exists; `scripts/
create_superadmin.py` is the headless equivalent if you would rather not use
the browser.

Then navigate to the `default` agent and click
**Test Agent** — this opens `admin-ui/components/TestAgentPanel.tsx`,
which talks directly to the webcall bridge over WebSocket and does its own
in-browser VAD (calibrated noise floor, barge-in support). Speak into your
mic; you should hear the agent's greeting and get real STT → LLM → TTS
turns with working interruption.

## 9. What's excluded here, and why

Real phone calls (Kamailio SIP routing, FreeSWITCH media, the C++
Gateway's `CallFSM`, the Vobiz PSTN bridge) are not reproducible from this
repo alone — Kamailio and FreeSWITCH are system-level installs with local
config outside version control, and the Gateway is a compiled native
service with its own build toolchain. If you have that stack already
running, `scripts/start_local.sh` covers it instead of this doc. Adding a
containerized/reproducible telephony stack is tracked as future work, not
started.
