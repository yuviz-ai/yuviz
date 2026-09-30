## What and why

<!-- One or two sentences: what this changes and why. Link the issue if there is one. -->

## How it was tested

- [ ] `pytest -m "not integration"` passes locally
- [ ] `pytest -m integration` passes locally (if DB/Redis code changed)
- [ ] Gateway `ctest` / Admin UI `npm run lint && npm run build` (if touched)
- [ ] Manually exercised: <!-- what you clicked / called -->

## Checklist

- [ ] New tests that open Postgres/Redis are marked `integration` (see `docs/ci-cd.md`)
- [ ] Tenant isolation considered: queries scoped by the verified token's tenant; cross-tenant access rejected
- [ ] No secrets, decrypted keys or `deployment/.env` contents in code, logs or this PR
- [ ] Schema changes are in the idempotent `database/*.sql` files

Integration tests run after an approving review; `CI / gate` runs on every push.
