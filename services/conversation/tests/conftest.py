"""Shared fixtures for the Conversation Service tests."""

from __future__ import annotations

import pytest

from services.conversation.workflow.runner import _GRAPH_CACHE


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    """graph_for() caches the parsed graph by (agent id, config_version), and
    every handler these tests build reuses both — without this, the first
    test's graph would be handed to all the rest."""
    _GRAPH_CACHE.clear()
    yield
    _GRAPH_CACHE.clear()
