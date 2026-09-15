"""libs/tenancy — the shared RLS scope reader and connection helpers.

Framework-free: no FastAPI, no service-specific import, so all six services
(and Conversation, which has no request scope at all) can depend on it
without importing each other's db.py.
"""
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
