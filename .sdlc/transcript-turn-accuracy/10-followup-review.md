# Code review (round 4, final)
VERDICT: GREEN
Coverage: callers 4 checked (cut_counted at `pipeline.py:101/1402/1417/1598`) · raises 0 new · promises 2 matched (one cut is one barge-in in both orders; the flag resets at turn start) · contracts 0 new · failure-visibility done · env ok (gateway C++ not compiled, unchanged since round 1)

Round-3 finding R3-1 (double count when the report lands before the apology's record) is FIXED.

The new test fails on pre-fix code:
- `test_a_cut_into_the_main_reply_while_the_apology_generates_counts_one_barge_in`, with `pipeline.py:1598` forced back to `count_barge_in=None`, fails `assert 2 == 1`.
- `pipeline.py` was restored from a scratch backup. Its sha1 3750a4c8 is identical before and after, and the test passes again.

cut_counted cannot hide a second, genuine barge-in:
- The flag is set only when a correction counted a cut of an already-recorded reply. That same cut cancels the in-flight apology, and the gateway sends one PlaybackFinished per cut.
- A completed main reply followed by a cut apology leaves the flag False, because no correction counts anything (the main entry is fully played or was already interrupted). The apology record then counts 1.
- The nested apology keeps the flag (`keep_pending=True`). The next turn start and the outer-loop apology reset it.

The idle-in-transaction check is now limited to the pool's backend pids (`test_transcript_builder.py:370-376`).
- Note, not a finding: it holds every current pool connection and then runs `pool.fetch`, which needs one more. It would block if the pool were already at max_size=5. That size is not reached in this test. Running the query on `conns[0]` would remove the dependency.

No open findings.
