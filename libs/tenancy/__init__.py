"""Shared, framework-free RLS scope reader and connection helpers."""
from .session import (
    TenantScope,
    TenantScopeConflict,
    TenantUnresolved,
    current_scope,
    current_tenant,
    platform_conn,
    set_caller_tenant,
    set_target_tenant,
    tenant_conn,
)

__all__ = [
    "TenantScope",
    "TenantScopeConflict",
    "TenantUnresolved",
    "current_scope",
    "current_tenant",
    "platform_conn",
    "set_caller_tenant",
    "set_target_tenant",
    "tenant_conn",
]
