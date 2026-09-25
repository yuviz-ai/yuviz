# Review: Unified Telephony Provider Service — design (revision 2)
VERDICT: GREEN

No findings. The latency revision is real, not just documented: I traced `deps.py:156-191` and
confirmed `assert_tenant_access` is `async def` with a UUID-branch string comparison at
lines 189-191 exactly as cited, `resolve_caller_tenant`'s slug→UUID mapping through the
`AccountStore` map correctly avoids the `tenants.get_tenant` Postgres branch. `gateway/` has zero
references to `telephony_configs` (grep across the whole tree returns nothing), corroborating the
"native path can't see this service" claim. `services/cloudonix/handoff.py`'s `HandoffStore` is
confirmed to be a plain in-process dict (`secrets.token_urlsafe(32)`, no Redis/Postgres), so the
WS-route "zero I/O" claim and the `test_hot_path_isolation.py` fixture that raises on every Redis
op are consistent with what the code actually does, not just what the design says it does.
`services/config/agents.py:47`'s `agent:{tenant_slug}:{agent_slug}` key, `libs/ratelimit.py`'s
in-process `FixedWindowCounter`, `admin-ui/app/telephony/page.tsx:305`'s DID-count heuristic being
replaced, and `services/vobiz/app.py:62-88`'s login/401-retry helper all match their cited
line numbers and behavior. The two named fixes (UUID-only `assert_tenant_access` call, and the
prewarmed `AccountStore` agent-ownership memo replacing a synchronous Config fallback reachable
from the outbound trigger) are both wired into the actual call sites in `auth.py`/`ownership.py`,
not just asserted in prose, and are each backed by a mechanical test
(`test_no_unawaited_guards.py`'s AST walk, `test_hot_path_isolation.py`'s route-dependency walk +
raising-Redis-client fixture) rather than a behavioral test that could pass for the wrong reason.

The PRD-vs-design pass surfaced nothing new: every AC has a design section and a named test: the
AC6 URL-shape deviation (server-minted WS token instead of the vendor's `provider_call_id`) is
disclosed and justified in Risks rather than silently substituted, the tenant-derivation tightening
that changes live Vobiz routing behavior is named as a risk with a concrete mitigation (verify the
Yuviz row before the regression call), and the SMS idempotency-key assumption from the PRD's open
question is carried through unchanged. No cited file, table, function or line number failed
verification.
