---
name: review-pr
description: Review a GitHub PR for correctness, tenant isolation, latency and design, then post line-by-line inline comments. Invoke with /review-pr <PR number|URL|branch> [dry-run] [request-changes].
disable-model-invocation: true
---

# /review-pr

Review the PR given in `$ARGUMENTS` (number, URL or branch).

## Orchestration (Cursor)

Task `pr-reviewer` in the foreground. Do not review in the parent chat.

## Steps

1. If `$ARGUMENTS` is empty, ask which PR — do not fall back to reviewing the working tree.
2. Check `gh auth status`. If not authenticated, stop and tell me to run `gh auth login`.
3. Task `pr-reviewer` in the foreground, passing the PR reference verbatim plus any flags I included (`dry-run`, `request-changes`).
4. Print only:

```
Verdict: <GREEN|AMBER|RED>
Blocking: <n>  Minor: <m>
<one line per finding>
Review: <url>
```

Pass `dry-run` through if I included it — in that case print the review instead of a URL, and confirm nothing was posted.
