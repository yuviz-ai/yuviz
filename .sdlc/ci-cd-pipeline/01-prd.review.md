# Review: 01-prd.md
VERDICT: AMBER

1. [blocking] AC1 is not a testable acceptance criterion: after this feature lands `.github` exists, so “workflow starts without any existing `.github` config having been present” cannot be verified on a PR — AC1 — fix: replace with “Given a PR targeting `main`, when it is opened or updated, then the CI workflow runs and reports check statuses on that PR.”
2. [blocking] Scope requires automated pytest/Gateway/Admin UI checks on pushes to `main` as well as PRs, but AC2–AC6 only bind those gates to PRs — Scope In / AC2–AC6 — fix: widen those ACs to “PR targeting `main` or push to `main`” (AC7’s “passes CI” alone does not define which jobs must run).
3. [minor] Scope requires publishing both commit-SHA and moving `main` tags; AC7 only requires the SHA tag — Scope In / AC7 — fix: add the `main` tag to AC7, or drop it from Scope if SHA-only is the decided contract.
