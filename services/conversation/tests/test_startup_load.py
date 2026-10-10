"""Startup load task: a failed model load / prewarm is logged, never silent."""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging

from services.conversation.__main__ import _await_stopped, _log_task_failure, _spawn, serve
from services.conversation.pipeline_config import SttConfig


def test_stt_env_fallback_default_is_the_cached_english_model():
    # Hosts without provider rows (native stack) have only small.en cached; load() is offline-only.
    assert SttConfig().model_size == "small.en"


async def test_failed_load_is_logged_at_error(caplog):
    async def load():
        raise RuntimeError("model not cached")

    task = asyncio.create_task(load(), name="startup load")
    task.add_done_callback(_log_task_failure)
    await asyncio.wait({task})
    await asyncio.sleep(0)  # callbacks run on the next loop iteration
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and errors[0].exc_info[1].args == ("model not cached",)


async def test_failed_heartbeat_is_logged_at_error_and_shutdown_join_completes(caplog):
    async def heartbeat():
        raise ConnectionError("redis down")

    task = asyncio.create_task(heartbeat(), name="heartbeat")
    task.add_done_callback(_log_task_failure)
    await asyncio.wait({task})
    await asyncio.sleep(0)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "heartbeat" in errors[0].getMessage()
    await _await_stopped(task)


async def test_cancelled_load_is_not_logged(caplog):
    task = asyncio.create_task(asyncio.sleep(60))
    task.add_done_callback(_log_task_failure)
    task.cancel()
    await asyncio.wait({task})
    await asyncio.sleep(0)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_shutdown_join_survives_a_failed_load():
    async def load():
        raise RuntimeError("model not cached")

    task = asyncio.create_task(load())
    await asyncio.wait({task})
    await _await_stopped(task)  # must not raise: stop()/server.stop() come after it

    cancelled = asyncio.create_task(asyncio.sleep(60))
    cancelled.cancel()
    await _await_stopped(cancelled)


async def test_spawn_logs_a_failing_task_by_name(caplog):
    async def load():
        raise RuntimeError("model not cached")

    task = _spawn(load(), "startup load")
    await asyncio.wait({task})
    await asyncio.sleep(0)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "startup load" in errors[0].getMessage()


def test_serve_starts_its_background_tasks_only_through_spawn():
    calls = [n for n in ast.walk(ast.parse(inspect.getsource(serve).lstrip()))
             if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))]
    bare = [n.lineno for n in calls if getattr(n.func, "attr", None) == "create_task"]
    assert bare == [], f"serve() calls create_task directly (line offsets {bare}); use _spawn"
    spawned = [n.args[1].value for n in calls if getattr(n.func, "id", None) == "_spawn"]
    assert sorted(spawned) == ["heartbeat", "startup load"]
