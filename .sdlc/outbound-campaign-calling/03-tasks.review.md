# Review: 03-tasks.md
VERDICT: AMBER

1. [blocking] ESL removal (T42/T44) has no dependency on T1 passing or on T38 (live Path A verification), so a failed prototype or an unverified rig still deletes the only working native-DID path; plan flag (b) claims otherwise but no task enforces it — Phase 5 header, T42, T44 — fix: add "(after T38)" to T42 and say T42-T44 are halted if T1 fails (Path B-only cutover variant).
2. [blocking] T17 makes every NullBillingAdmission method except `account_status` raise, but the protocol has `aclose()` (02-design.md:577) and T43 requires the lifespan to close it and assert that via spies, so app shutdown crashes with billing off — T17, T43 — fix: T17 exempts `aclose` (no-op) alongside `account_status` and tests it.
3. [blocking] The superadmin "Campaign compliance" card on `tenants/page.tsx` (platform default retention, number series per purpose) in the design Changes table has no UI task; T50 covers only the capacity card, and T66 is API only — T50, T77 — fix: add a Phase 7 task (after T66) for the compliance card in `tenants/page.tsx`, with a browser-driven done-when.
4. [minor] T60 is marked `[P: T59, T62, T64]` while T62 is `(after T60)` and imports `CURRENT_TURN_ID`, so the parallel batch is self-contradictory — T60, T62 — fix: drop T62 from T60's P-list.
5. [minor] Design changes `get_progress` to return `spent_minor`, `budget_limit_minor` and `billing_currency`, but T46 omits them and no deferred task (T78-T88) picks them up — T46, Phase 8 — fix: add them to T81 or T86 as DEFERRED.
