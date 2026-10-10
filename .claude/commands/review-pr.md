---
description: Review a PR for correctness, tenant isolation, latency and design, post it with line-by-line inline comments, and merge it if GREEN
allowed-tools: Bash, Read, Agent
---

Review the PR given in `$ARGUMENTS` (number, URL or branch).

1. If `$ARGUMENTS` is empty, ask which PR — do not fall back to reviewing the working tree.
2. Check `gh auth status`. If not authenticated, stop and tell me to run `gh auth login`.
3. Spawn `pr-reviewer` in the foreground, passing the PR reference verbatim plus any flags I included (`dry-run`, `request-changes`, `no-merge`).
4. Print only:

```
Verdict: <GREEN|AMBER|RED>
Blocking: <n>  Minor: <m>
<one line per finding>
Review: <url>
Merged: <merge-commit sha | no: reason>
```

Pass `dry-run` through if I included it — in that case print the review instead of a URL, and confirm nothing was posted or merged.
