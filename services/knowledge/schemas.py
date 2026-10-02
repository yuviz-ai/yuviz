"""Pydantic request models for Knowledge Service; responses are plain dicts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class KnowledgeBaseCreate(BaseModel):
    slug: str
    name: str
    description: str = ""
    embedding_config_id: str | None = None


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    embedding_config_id: str | None = None
    status: str | None = None


class DocumentUpdate(BaseModel):
    title: str | None = None
    language: str | None = None
    tags: dict[str, Any] | None = None
    # 'auto': retrieved when relevant; 'prompt': injected every turn.
    usage_mode: str | None = None


class AgentKnowledgeBaseCreate(BaseModel):
    kb_id: str
    enabled: bool = True


class AgentKnowledgeBaseUpdate(BaseModel):
    enabled: bool


class RetrieveRequest(BaseModel):
    tenant_slug: str
    agent_slug: str
    query: str
    # None = no override (falls back to the agent's policy, then system default).
    top_k: int | None = None
    max_tokens: int | None = None
    minimum_score: float | None = None
    rerank: bool | None = None
    hybrid_search: bool | None = None
    include_citations: bool | None = None


class RetrievalPolicyUpdate(BaseModel):
    top_k: int | None = None
    max_tokens: int | None = None
    minimum_score: float | None = None
    rerank: bool | None = None
    hybrid_search: bool | None = None
    include_citations: bool | None = None
