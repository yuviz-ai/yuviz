# Review: one-click-crm-integrations 02-design.md (round 2)
VERDICT: GREEN

Round-1 findings 1-4 are resolved; finding 5 is resolved. The design is BLOCKED on connector-presets-oauth by its own banner, so "GREEN" means the design is correct to build once that gate clears, not that it may be built now.

1. [minor] The cal.com preset row is declared ("calcom_scheduling", one row for v1, gated) but the Preset rows table gives no row for it (name, endpoint, params). PRD 35-38 define check-availability and booking, and v1 is read-only. Nothing is built wrong, because the entry ships dark. — Preset rows — fix: add one line saying the cal.com row set is deferred with the entry, or list its read-only availability row.
2. [minor] Risks says HubSpot is "covered by the two OR-ed filter groups". That overclaims. HubSpot `EQ` matches the stored string exactly, so a contact stored as `(555) 123-4567` is never returned and the suffix match cannot recover it, the same limitation as Zoho. — Risks (Zoho bullet) — fix: add HubSpot to the exact-match limitation bullet.

Verification of the four:
- Finding 1 resolved. `provider_host_allowed` branches on the row's `endpoint_base_source`. The upsert's `DO UPDATE SET` gains `api_base_url` and `auth_kind`. A NULL origin fails `oauth_connection` rows closed and leaves `literal` rows working. Test 2 covers both row kinds on one connection.
- Finding 2 resolved. cal.com keeps `oauth2_authorization_code`, so the CHECK and composite FK hold. `_CREDENTIAL_REF_FIELDS` stays empty, so no `auth_config` ciphertext is read. D4 is named and the count is four.
- Finding 3 resolved. The BLOCKED banner is present. The amendment is a precise before/after table, and I confirmed the cited lines 56, 57 and 214 in connector-presets 02-design.md match the quoted text. The inherited round-4 findings are in Risks, and the cited transform hook already takes `caller_ani` (that design, line 507), so the executor wiring is not a gap.
- Finding 4 resolved. `value_format` applied before `value_prefix`, the Salesforce digits and HubSpot dual-group encoding, the suffix match with a 7-digit floor, `ambiguous` with empty `items`, and the Zoho limitation are all specified. Test 3 covers `(555) 123-4567`.

`value_format` is the minimum for this problem. It is a closed two-value set, preset-only, constrained to `source='caller_id'`, and a free-form template was explicitly rejected. It pulls in no scope beyond the column and one CHECK.

No PRD open question has been silently resolved. OQ1-OQ8 are each marked answered, assumed, or deferred, with a Risks entry.
