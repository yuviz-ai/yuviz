# Security review: feature/outbound-campaign-calling (round 6, `git diff af99c22..c0ca5fb`, E.164 upload fix)
VERDICT: AMBER

0 critical, 0 high, 1 medium, 0 low. Prior findings: 0 fixed, 1 carried, 0 accepted.
- R5-1 [medium] Gateway trusts `tenant_slug`/`agent_slug` from the unauthenticated metadata frame: CARRIED, NOT FIXED. The brief says it was fixed with a shared secret that FreeSWITCH writes and the Gateway checks. No such code exists. `git diff af99c22 HEAD` changes nothing under `gateway/`, `scripts/freeswitch/` or `deployment/`. `CallMetadata::direct_route` (`gateway/src/config/Config.cpp:336-339`) still returns the frame's slugs whenever `direction == "outbound"`, `Application.cpp:223-226` still uses them without calling `resolve_route`, and `start_voice_ai.lua:36-46` puts no secret or MAC in the frame. The gateway Redis user has no `campaign:*` key it could check a route against (`users.acl.tpl:3`).

## Findings
1. [medium] Unauthenticated agent selection over the Gateway audio WebSocket. Same issue as R5-1, and the code has not changed. — `gateway/src/config/Config.cpp:336-339`, `gateway/src/core/Application.cpp:223-226`
   Attack: any peer that can reach the Gateway WS can do this. With the default bind that is any local process. With `GATEWAY_LISTEN_HOST=HOST_IP` in the split-VM setup it is anyone on the VM network. The peer opens `/voice/<uuid>` and sends `{"direction":"outbound","tenant_slug":"<victim>","agent_slug":"<any>"}`. It can then talk to any active agent of any tenant, including agents bound to no DID, and draw out that agent's prompt and KB. The STT, LLM and TTS cost is billed to the victim tenant.
   Fix: honour `direct_route` only when it matches server-side state. Option one: FS or Campaigns writes a short-TTL `campaign:route:{call_uuid}`, and the gateway user gets `+get` on it. Option two: an HMAC over the metadata with a secret shared by FS and the Gateway, compared in constant time, with the call uuid and a timestamp inside the MAC.

## Verified controls
E.164 change (`services/campaigns/campaign_contacts.py:20-30,62-70`):
- Upload normalisation uses the same `_E164` regex (`dial_request.py:29`) that `DialIntent` enforces at dial time. Nothing accepted at upload can get past the dial-time validation and into the originate string, Kamailio or Lua. The input is digits and `+` only, so no injection is possible.
- DNC matching is unaffected. `dnc.normalize_phone` compares the last 10 digits (`dnc.py:12-15`). Normalisation only adds `+` or drops one leading `0`, so a number already on the list still matches at upload (`routers/campaigns.py:139-142`) and before dialing (`worker.py:251`).
- The upload error names only row numbers. It echoes no phone number or name back.

Round-5 controls, re-read at HEAD. None of their files changed since af99c22:
- `direct_route` is gated on `direction == "outbound"`. The Lua script sends `outbound` and the slugs only when `campaign_key` is set, and that variable comes only from originate vars, never from SIP (`start_voice_ai.lua:36-46`).
- `resolve_outbound_route` sets `agent_ok` only when `a.tenant_id = t.id`, the agent is active and it is not deleted (`campaigns.py:207-215`). The worker refuses to dial unless both `caller_id_owned` and `agent_ok` hold (`worker.py:229`).
- Slug validation is identical at every hop: Python `_SLUG` (`dial_request.py:30`), the Kamailio regex with a canonical rebuild, and Lua `sip_token`.
- `campaign_event.lua` returns early for channels without `campaign_key`, filters its fields before the string build, and writes only to `campaign:events:fs`.
- Redis ACL: `default` is off. FreeSWITCH has only `getdel` on intents, `set` on claims and `xadd` on the fs stream. Telephony has `+get` only on `~agent:* ~did:*` (`users.acl.tpl:2,8,10`). Correction to the R5 wording: the `campaigns` user's key patterns sit at its root, not in a selector. They are limited to `campaign:*`, so the control still holds.
- DB tenant isolation: RLS FORCE is on for the four campaign tables, and platform-connection attempt reads pin `tenant_id`.
- Route authorization: `bind_path_tenant` and `require_path_tenant_access` apply, and mutations require admin. A foreign DNC id gets the same 404 as a missing one.
- Kamailio `^100[0-2]$` is anchored at all four sites (`kamailio.cfg.tpl:733,797,821,854`).
- Egress token HMAC with single use and expiry, the dial-handler HMAC over raw `$rb`, the webhook signature checked before the nonce, and `tenant_id` taken from the verified account: all unchanged since af99c22.
