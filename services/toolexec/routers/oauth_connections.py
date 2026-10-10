"""
services/toolexec/routers/oauth_connections.py — per-tenant OAuth connector
routes. The tenant is always the path's, never a body field. Reads sit behind
`get_current_user`, writes behind `require_role("superadmin","admin")` (a
viewer or supervisor must not connect or sever a tenant's CRM or calendar),
and every handler awaits `assert_tenant_access` first (lesson 38).

The provider redirects the browser to the authenticated console, which POSTs
the callback here, so no unauthenticated route exists (lesson 1). Every
callback failure returns the same 400 body whatever the cause.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from services.config.auth import CurrentUser
from services.config.deps import (
    assert_tenant_access,
    bind_path_tenant,
    get_current_user,
    require_path_tenant_access,
    require_role,
)

from .. import oauth
from ..schemas import ApiKeyConnectRequest, OAuthAuthorizeRequest, OAuthCallbackRequest

tenant_scoped_router = APIRouter(
    prefix="/tenants/{tenant_id}/oauth-connections",
    tags=["oauth_connections"],
    dependencies=[Depends(bind_path_tenant), Depends(require_path_tenant_access)],
)
router = APIRouter(tags=["oauth_connections"])


@router.get("/oauth-providers")
async def list_oauth_providers(current_user: CurrentUser = Depends(get_current_user)):
    return [
        {"key": key, "label": oauth.PROVIDERS[key].label, "auth_kind": oauth.PROVIDERS[key].auth_kind}
        for key in oauth.configured_providers()
    ]


@tenant_scoped_router.get("")
async def list_oauth_connections(tenant_id: str, current_user: CurrentUser = Depends(get_current_user)):
    await assert_tenant_access(tenant_id, current_user)
    return await oauth.list_connections(tenant_id)


@tenant_scoped_router.post("/callback")
async def oauth_callback(
    tenant_id: str,
    body: OAuthCallbackRequest,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await assert_tenant_access(tenant_id, current_user)
    try:
        return await oauth.complete_authorization(
            tenant_id=tenant_id, user_id=current_user.id, user_email=current_user.email,
            state=body.state, code=body.code, accounts_server=body.accounts_server,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="oauth_connection_failed") from None


@tenant_scoped_router.post("/{provider}/authorize")
async def authorize_oauth_connection(
    tenant_id: str,
    provider: str,
    body: OAuthAuthorizeRequest,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await assert_tenant_access(tenant_id, current_user)
    authorize_url = await oauth.start_authorization(
        tenant_id=tenant_id, user_id=current_user.id, provider=provider, preset_key=body.preset_key,
    )
    return {"authorize_url": authorize_url}


@tenant_scoped_router.post("/{provider}/api-key")
async def connect_api_key_connection(
    tenant_id: str,
    provider: str,
    body: ApiKeyConnectRequest,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await assert_tenant_access(tenant_id, current_user)
    try:
        return await oauth.connect_api_key(
            tenant_id=tenant_id, provider=provider, api_key=body.api_key.get_secret_value(),
            user_id=current_user.id, user_email=current_user.email,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="oauth_connection_failed") from None


@tenant_scoped_router.delete("/{connection_id}")
async def disconnect_oauth_connection(
    tenant_id: str,
    connection_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    await assert_tenant_access(tenant_id, current_user)
    return await oauth.disconnect(
        tenant_id=tenant_id, connection_id=str(connection_id), user_id=current_user.id,
        user_email=current_user.email, background_tasks=background_tasks,
    )
