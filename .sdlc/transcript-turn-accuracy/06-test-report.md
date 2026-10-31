# Test report
COMMAND: venv/bin/python -m pytest -q services/conversation/tests/test_servicer_spoken_record.py services/conversation/tests/{test_pipeline,test_callflow_handler,test_turn_persistence,test_hangup_with_concurrent_reader,test_transcript_builder,test_session_finalizer}.py services/config/tests/test_calls.py
RESULT: 353 passed, 0 failed

- barge-in drained during synthesis stores only the sent sentence (servicer-level, real drain) — pass
- hang-up during transfer-failed apology persisted before session end (servicer teardown aclose) — pass
- (existing) flow disposition tripwire scans runner.py + handler.py for exact 4 reasons — pass
- (existing) pipeline GeneratorExit apology / fallback tests — pass
FAILURES: None
UNCOVERED: None new. Plain-async-for transfer-refused apology at pipeline.py:1309 (not aclosing) not driven through the servicer finalizer.
