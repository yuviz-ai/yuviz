from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from .. import users as users_service
from ..auth import CurrentUser, create_access_token
from ..deps import get_authenticated_user, is_platform_scoped
from ..schemas import BootstrapRequest, ChangePasswordRequest, LoginRequest

router = APIRouter(prefix="/auth", tags=["auth"])


def _token_response(user: dict) -> dict:
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": users_service.to_public_dict(user),
    }


@router.get("/setup-status")
async def setup_status():
    return {"setup_required": not await users_service.superadmin_exists()}


@router.post("/bootstrap", status_code=201)
async def bootstrap(body: BootstrapRequest):
    try:
        user = await users_service.bootstrap_first_superadmin(
            email=body.email, password=body.password,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="that email is already registered")
    if user is None:
        raise HTTPException(status_code=409, detail="setup has already been completed")
    return _token_response(user)


@router.post("/login")
async def login(body: LoginRequest):
    user = await users_service.authenticate(body.email, body.password)
    if user is None:
        # Same 401 for "no such email" and "wrong password" — see
        # users.authenticate()'s docstring for why.
        raise HTTPException(status_code=401, detail="invalid email or password")
    return _token_response(user)


@router.get("/me")
async def me(current_user: CurrentUser = Depends(get_authenticated_user)):
    user = await users_service.get_user_by_id(current_user.id)
    if user is None:
        # Token is validly signed but the user row is gone (deleted since
        # the token was issued) — same posture as an expired token: 401, not
        # a 404 that would leak whether the id ever existed.
        raise HTTPException(status_code=401, detail="user no longer exists")
    return users_service.to_public_dict(user)


@router.post("/change-password", status_code=204)
async def change_password(
    body: ChangePasswordRequest, current_user: CurrentUser = Depends(get_authenticated_user),
):
    # Always the caller's own password — there is no "change someone else's
    # password" endpoint. An admin resetting another user's credentials is a
    # different, not-yet-built operation (would need its own audit shape:
    # "admin X reset user Y's password" is a materially different event from
    # "user Y changed their own password").
    ok = await users_service.change_password(
        current_user.id,
        current_password=body.current_password,
        new_password=body.new_password,
        platform_scoped=is_platform_scoped(current_user),
    )
    if not ok:
        raise HTTPException(status_code=400, detail="current password is incorrect")
