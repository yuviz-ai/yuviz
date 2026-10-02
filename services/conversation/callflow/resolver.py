"""Resolve an agent's call flow into (CallFlowGraph, CallFlow). Never raises; None
means fall through to the conversational agent."""

from __future__ import annotations

import logging

from libs.config_sdk import IConfigProvider, RuntimeConfig
from libs.config_sdk.callflow import CallFlowGraph
from libs.config_sdk.models import CallFlow

from .runner import graph_for_flow

log = logging.getLogger(__name__)


async def resolve_call_flow(
    runtime_config: RuntimeConfig, config: IConfigProvider,
) -> tuple[CallFlowGraph, CallFlow] | None:
    call_flow_id = runtime_config.agent.call_flow_id
    if not call_flow_id:
        return None

    try:
        flow = await config.get_call_flow(runtime_config.tenant.slug, call_flow_id)
        if flow is None:
            log.info(
                "resolve_call_flow: no published flow tenant=%s call_flow_id=%s "
                "— falling back to the conversational agent",
                runtime_config.tenant.slug, call_flow_id,
            )
            return None

        graph = graph_for_flow(flow)
        return graph, flow
    except Exception:
        log.exception(
            "resolve_call_flow: resolution failed tenant=%s call_flow_id=%s "
            "— falling back to the conversational agent",
            runtime_config.tenant.slug, call_flow_id,
        )
        return None
