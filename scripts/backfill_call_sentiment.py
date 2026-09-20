#!/usr/bin/env python3
"""
Scores calls that already ended, using the same SentimentScorer the
Conversation Service runs at end_call (services/conversation/sentiment.py).

Sentiment is written when a call ENDS, so every call that finished before
that shipped has calls.sentiment NULL forever — this backfills them from
the transcripts already in Postgres. Nothing else does: there is no retry
sweep, by design (a scorer that failed once for a real reason should not
silently re-bill on every restart).

Only touches rows where sentiment IS NULL, so it is safe to re-run and will
never overwrite a score the live path already wrote. Calls with no
transcript turns are skipped and stay NULL — "never scored" is the honest
value for a call with no caller speech to read.

Usage:
  python3 scripts/backfill_call_sentiment.py [--limit N] [--tenant SLUG] [--dry-run]

Requires: POSTGRES_DSN, and VOICEAI_SENTIMENT_API_KEY or OPENAI_API_KEY
(see services/conversation/pipeline_config.py's SentimentConfig). Costs one
model call per scored call — use --limit first to see what you are in for.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402

from services.conversation.pipeline_config import SentimentConfig  # noqa: E402
from services.conversation.providers.llm.openai import OpenAILLM  # noqa: E402
from services.conversation.sentiment import SentimentScorer, Turn  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="score at most N calls")
    parser.add_argument("--tenant", default=None, help="restrict to one tenant slug")
    parser.add_argument("--dry-run", action="store_true", help="score but do not write")
    args = parser.parse_args()

    dsn = os.environ.get("POSTGRES_DSN")
    if not dsn:
        print("POSTGRES_DSN is not set", file=sys.stderr)
        return 1

    cfg = SentimentConfig()
    if not cfg.api_key:
        print(
            "No scoring key — set VOICEAI_SENTIMENT_API_KEY or OPENAI_API_KEY.",
            file=sys.stderr,
        )
        return 1

    where = ["sentiment IS NULL", "ended_at IS NOT NULL"]
    params: list[object] = []
    if args.tenant:
        params.append(args.tenant)
        where.append(f"tenant_id = ${len(params)}")
    sql = f"SELECT session_id FROM calls WHERE {' AND '.join(where)} ORDER BY started_at DESC"
    if args.limit:
        params.append(args.limit)
        sql += f" LIMIT ${len(params)}"

    # Connects with whatever POSTGRES_DSN names — a maintenance script run by
    # hand, the same posture as the other scripts here. It reads and writes
    # across tenants by design; that is the job.
    pool = await asyncpg.create_pool(dsn)
    llm = OpenAILLM(
        api_key=cfg.api_key, model=cfg.model, system="",
        temperature=0.0, base_url=cfg.base_url, timeout_s=cfg.timeout_s,
    )
    scorer = SentimentScorer(llm, max_turns=cfg.max_turns, timeout_s=cfg.timeout_s)

    scored = skipped = failed = 0
    try:
        session_ids = [r["session_id"] for r in await pool.fetch(sql, *params)]
        print(f"{len(session_ids)} unscored call(s); model={cfg.model}"
              f"{' (dry run)' if args.dry_run else ''}")

        for session_id in session_ids:
            rows = await pool.fetch(
                "SELECT caller_text, ai_response FROM transcript_entries "
                "WHERE session_id = $1 ORDER BY turn_number",
                session_id,
            )
            turns = [Turn(caller_text=r["caller_text"], ai_response=r["ai_response"]) for r in rows]
            if not turns:
                skipped += 1
                continue

            result = await scorer.score(turns)
            if result is None:
                failed += 1
                print(f"  {session_id[:8]}  not scored")
                continue

            scored += 1
            print(f"  {session_id[:8]}  {result.label:<11} {result.reason}")
            if not args.dry_run:
                await pool.execute(
                    "UPDATE calls SET sentiment = $2, sentiment_reason = $3 "
                    "WHERE session_id = $1 AND sentiment IS NULL",
                    session_id, result.label, result.reason or None,
                )
    finally:
        await llm.aclose()
        await pool.close()

    print(f"\nscored {scored}, skipped {skipped} (no transcript), not scored {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
