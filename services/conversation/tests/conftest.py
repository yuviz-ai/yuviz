"""Shared fixtures for the Conversation Service tests."""

from __future__ import annotations

import pytest

from services.conversation.callflow.runner import _FLOW_CACHE
from services.conversation.workflow.runner import _GRAPH_CACHE


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    """graph_for()/graph_for_flow() cache the parsed graph by
    (agent/tenant id, config_version), and every handler these tests build
    reuses both — without this, the first test's graph would be handed to
    all the rest."""
    _GRAPH_CACHE.clear()
    _FLOW_CACHE.clear()
    yield
    _GRAPH_CACHE.clear()
    _FLOW_CACHE.clear()
