---
description: Check every open PR, review the ones with new commits, and merge the ones that come back GREEN
allowed-tools: Bash, Read, Agent
---

Review every open PR that needs it, and let `pr-reviewer` merge the GREEN ones. Never stop to ask whether to review a PR: reviewing is this command's job. An interrupted or failed run is not a reason to skip a PR next time. If a GitHub call fails, retry it and don't report the PR as waiting on me. Flags in `$ARGUMENTS` (`dry-run`, `no-merge`) are passed to every reviewer.

1. Check `gh auth status`. If not authenticated, stop and tell me to run `gh auth login`.
2. List the open PRs and who you are:

   ```bash
   gh api user --jq .login
   gh pr list --state open --json number,title,isDraft,headRefOid,baseRefName,mergeStateStatus
   ```

3. For each PR, find the last review you posted (`gh api repos/<owner>/<repo>/pulls/<N>/reviews --paginate`, filtered to your login) and pick an action:
   - **Draft:** skip.
   - **Never reviewed by you, or the head moved since your last review:** review it.
   - **Head unchanged, last verdict GREEN, still open:** run the reviewer anyway. It goes straight to the merge check, so a PR a human approved since the last run gets merged.
   - **Head unchanged, last verdict AMBER or RED:** skip, as waiting on the author.
4. Spawn one `pr-reviewer` per PR to review, in the foreground and in parallel, at most 5 at a time. Pass the PR number plus my flags. The reviewer works out whether it is a follow-up and keeps out of the working tree on its own.
5. Print only this:

```
PR  | Action            | Verdict | Blocking/Minor | Merged          | Review
57  | follow-up review  | RED     | 1/1            | no: not GREEN   | <url>
58  | skipped           | AMBER   | -              | waiting on author | <url>
```

Then print one line per PR that came back GREEN but did not merge, with the reason. `waiting for approval` means a human has to approve it on GitHub; it merges on the next run. If nothing needed review, print `No open PRs need review.`
