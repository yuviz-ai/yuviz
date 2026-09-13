# Security review: .sdlc/evaluate-pipecat/02-design.md
VERDICT: RED

Round 4 — focused re-check of round 3's finding 1 (the design republished the private-doc inventory while
specifying its own redaction). This file is itself in scope for the design's publication boundary
(Interfaces #5), so it refers to redacted citations by **count and pointer only**, never by name.

Round 3 finding 1 is **RESOLVED for `02-design.md`**. Verified mechanically: a grep for every one of the
private memory-doc filenames previously enumerated in Changes row 2 and Interfaces #5 returns **zero hits**
in `02-design.md`, and zero in `00-request.md`, `01-prd.review.md`, `02-design.review.md` and this file.
The count-and-pointer form ("N citations, mapping in scratch") does not leak: a count is not an inventory
and no name is derivable from it. The rule at Interfaces #5 (lines 118–124) is now stated self-applyingly
and binds this file too. `01-prd.md` still carries the un-redacted citations — that is the known open
remediation, gated as precondition (a0), not a regression.

The fix is sound in substance. It does, however, move the whole secret into one uncontrolled file and it is
gated by an enumeration command that returns nothing on the run that matters. Both are below.

## Findings

1. [high] Check 6's file enumeration is vacuous on the only run that matters — the pre-commit sweep of a
   directory that is entirely untracked. — design §Test plan check 6, lines 242–247
   Verified on this working tree: `git ls-files .sdlc/evaluate-pipecat/` returns **nothing** (the directory
   has never been committed), and `git status --porcelain -- .sdlc/evaluate-pipecat/` returns exactly one
   line, `?? .sdlc/evaluate-pipecat/` — git collapses an untracked directory to a single entry unless
   `-uall` is passed. So the union the check specifies resolves to one directory string and zero files.
   Attack: the implementer runs check 6 exactly as written before `git add`, gets an empty file list, every
   sub-check passes over nothing including (a0), and the directory is committed with `01-prd.md`'s
   un-redacted private citations intact. The one control standing between private operational memory and a
   permanent public history reports success without reading a single file (lessons #12, #14).
   Fix: enumerate with `git status --porcelain -uall -- .sdlc/evaluate-pipecat/` (verified: lists all six
   files individually), or stage first and use `git diff --cached --name-only`. State that an empty file
   list is a blocking fail, not a pass.

2. [high] The scratch redaction map is now the single concentrated copy of everything the boundary excludes
   — all private names, each paired with its replacement, plus the deny-sweep filename patterns 6(b)
   deliberately omits — and its location is specified only as a bare relative path with no enforcement.
   — design §Changes row 3 (line 38), §Interfaces #5 (line 121), §Test plan check 6(a0)/(b)
   The design calls it "outside repo, uncommitted" but gives no absolute path; `.gitignore` has no entry for
   it (verified); check 6's scope is limited to `.sdlc/evaluate-pipecat/`, so a copy at repo root is outside
   the sweep; and (a0)/(b) explicitly *exempt* tokens naming that path, so a reviewer applying the rule
   literally will not flag a pointer to it.
   Attack: the implementer creates `pipecat-evidence/redaction-map.md` relative to the repo root — the
   plainest reading of the path as written — and commits with `git add -A` or a `git add .sdlc/ .` style
   line. The public PR now contains one file holding the complete private-name inventory, the name→
   replacement pairing, and the pattern list, i.e. strictly more than the leak this round fixed, and
   unretractable once pushed.
   Fix: pin the map to an absolute path outside the repo tree (the agent scratchpad dir), and add
   `pipecat-evidence/` to `.gitignore` as a belt-and-braces second control. Add a check 6 arm: `git status
   --porcelain -uall` over the **repo root** must show no `pipecat-evidence/` path.

3. [medium] (Carried forward from round 3, unchanged.) The (a0) precondition is scoped to `` `*.md` ``
   tokens, but the disclosure in `01-prd.md` is carried as much by the *glosses* as by the filenames — and
   Changes row 2 bounds the pass to "citation form only … Requirements, ACs and scope are unchanged", which
   forbids editing them. Concrete instance: the PRD describes this codebase's "in-progress, not-yet-merged"
   workflow-graph effort (How-this-is-solved-elsewhere, Scope, and AC14, which makes it a required
   comparison target). `git grep` finds zero tracked mention of a workflow graph model or roadmap — only the
   shipped `services/conversation/workflow/runner.py` and `libs/config_sdk/workflow.py`. Strip the filename
   and the sentence still publishes an unreleased architectural direction and the fact that it is stalled;
   check 6(a) then blocks the commit with no remedy available inside the citation-form-only bound.
   — design §Changes row 2 vs §Test plan check 6(a)
   Attack: any reader of the public repo learns an unshipped architectural direction and its stalled status,
   and from the same sentence that a private planning corpus exists behind it.
   Fix: say in Changes row 2 that the pass also converts private *facts* in the surrounding gloss, not just
   the citation token — each becomes a tracked-file citation or `[detail withheld — public repo]`, AC number
   retained (lessons #7) — and point AC14's comparison target at the two tracked workflow modules.

4. [low] The mechanical arms of the sweep catch a private doc only when it appears as a backticked `*.md`
   token. Interfaces #5's rule correctly says "**titles** or filenames", but neither (a0) nor (b) can detect
   a title-form or extensionless citation ("the DID-cache memory note", "the coding-rules doc"), leaving
   that case to (a)'s human fact test alone. — design §Test plan check 6(a0), 6(b)
   Attack: a later stage's `03-tasks.md` or `05-review.md` cites a private doc by title while explaining why
   a requirement exists; both mechanical arms pass, and the inventory fact ("a private doc about X exists")
   is published anyway.
   Fix: add an arm that greps the in-scope files for the private-doc naming shape held in scratch (the
   patterns are already there per Changes row 3) rather than relying on the `.md` extension alone.

5. [low] (Carried forward from round 3, unchanged.) Check 6 leaves no artifact — nothing records that it
   ran, over which file set, or with what result. — design §Test plan check 6
   Attack: an implementer commits the directory without running the sweep; the leak is in history and
   survives any later delete. No reviewer can tell from the repo whether the gate executed.
   Fix: require the command set and its output over the enumerated file list to be pasted into the stage's
   review artifact before `git add` — which also makes finding 1's empty file list visible.

## Verified controls
- **Round 3 finding 1 closed for the design**: none of the previously enumerated private memory-doc
  filenames appears anywhere in `02-design.md` (grep, zero hits), and the two passages that carried them
  (Changes row 2, Interfaces #5 final paragraph) now use count-and-pointer form.
- **No relocation this round**: the same grep returns zero hits across `00-request.md`, `01-prd.review.md`,
  `02-design.review.md` and this file. `01-prd.md` alone still carries them, which is the declared open
  remediation rather than a new leak — the pattern of the finding migrating one file over each round
  (lessons #31) did not recur.
- Count-and-pointer form does not itself disclose: "N citations, mapping in scratch" yields no name, no
  subject and no derivable inventory; the count matches what `01-prd.md` actually contains.
- The rule at Interfaces #5 is genuinely self-applying as claimed — it names this design, `02-security.md`
  and every `*.review.md` as bound on identical terms, so no artifact is exempt for being an "input".
- (a0)'s test is the right shape and can fail: `git ls-files --error-unmatch` resolves `.sdlc/lessons.md`
  and does not resolve a bare private filename, so the check discriminates rather than passing everything —
  when it is given a file list at all (finding 1).
- Scope of the boundary is the whole directory including files later stages add, not the deliverable alone.
- Repo exposure claims re-verified: `.sdlc/` is tracked, `.sdlc/evaluate-pipecat/` is not ignored, so the
  first commit publishes every file in it at once, as the design assumes.
- Already-tracked-or-not claims spot-checked and still correct: the `did:{did}` routing key in `CURSOR.md`,
  `_AUDIO_DELAY_S` in `services/vobiz/bridge.py`, the Kamailio dialplan pattern in
  `scripts/kamailio/kamailio.cfg.tpl` — all tracked and therefore publishable; the demo tenant slug on the
  deny list returns zero `git grep` hits and is correctly excluded.
- Interfaces #4 cites only tracked files and states the routing-miss rule as a requirement without
  describing current fallback behavior — consistent with exclusion (c).
- No runtime surface: no schema, endpoint, query, cache key, token or auth decision is introduced. Tenant
  isolation, privilege escalation, IDOR and injection are not reachable from this change; the entire attack
  surface remains information disclosure into a permanent public history.
