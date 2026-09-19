# Review: 01-prd.md (Live Calls Monitoring) — Round 2
VERDICT: GREEN

No findings. Verified the four round-1 resolutions landed cleanly and consistently:

1. `tenants.max_concurrent_calls` is stated as a schema addition in Constraints ("Schema addition
   required: a new `tenants.max_concurrent_calls` column…mirroring the existing per-campaign
   `campaigns.max_concurrent_calls` column"), not merely asserted inside AC13/AC16. Confirmed
   `campaigns.max_concurrent_calls` exists in `database/schema.sql` and
   `services/campaigns/campaigns.py`.
2. Platform-scoped (NULL-tenant) access is consistent everywhere it appears: Scope explicitly
   lists "no aggregated cross-tenant view" as Out; AC2 requires selecting a single tenant first,
   gated on role/identity not on NULL-tenant status; AC10 folds platform-scoped denial into the
   same tenant-isolation guarantee "per AC2"; Constraints restates the one-tenant-at-a-time rule.
   No AC or Constraint contradicts this.
3. Sentiment is now a stated design-stage responsibility in Constraints — the architect must check
   whether `transcript_entries`/Conversation Service already computes a sentiment-like signal, and
   either wire in a minimal one or explicitly scope it out in the design doc — rather than AC4
   silently assuming a computed signal exists. `transcript_entries` and related Conversation code
   confirmed present in `services/conversation/`.
4. Refresh interval is a hard 5 seconds everywhere timing is mentioned (Scope, AC5, AC6, AC7,
   AC13, AC14, AC16, Constraints' polling-load paragraph). No leftover "next cycle" or unstated
   timing language found.

Open Questions section reads "None." — correctly resolved, and no fifth question was introduced
by these changes.
