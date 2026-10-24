# Owner decisions not yet applied to 01-prd.md / 02-design.md (2026-10-20)

1. Path A trigger = JSON over HTTP to Kamailio, replacing the Campaigns SIP client / trigger INVITE.
   - Campaigns POSTs a JSON dial request (callee, caller_id, agent_did, campaign_id, contact_id, idempotency key) to a dedicated Kamailio `xhttp` handler that can only dial.
   - Handler: loopback-only bind, source allowlist, shared-secret/HMAC check in config; JSON parsed/validated with `jansson`.
   - Kamailio selects a FreeSWITCH node via the same dispatcher as inbound and itself sends the INVITE (`uac_req_send` / `t_uac`).
   - `jsonrpcs` is NOT exposed to Campaigns (admin interface).
   - Outcomes still return via the FreeSWITCH -> Redis `campaign:events` stream; Kamailio cannot track call results.
   - Drop `services/campaigns/sip_trigger.py`; Task 0 prototype re-targets the Kamailio-originated INVITE.
2. Still to apply with it: the 3 HIGH security findings in 02-security.md (dial intent consumed atomically from the trust store, non-spoofable PSTN egress for the FreeSWITCH callee leg, Redis auth with per-client ACLs).
