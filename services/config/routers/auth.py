from __future__ import annotations

import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import email, google_oauth, verification
from .. import users as users_service
from ..auth import CurrentUser, create_access_token
from ..deps import fresh_console_authority, forget_user, get_authenticated_user, is_platform_scoped
from ..google_oauth import GoogleOAuthError
from ..schemas import (
    ChangeEmailRequest, ChangePasswordRequest, ConfirmEmailChangeRequest, ForgotPasswordRequest, LoginRequest,
    RegisterRequest, ResendCodeRequest, ResetPasswordRequest, VerifyEmailRequest,
)
from ..verification import CodeError, EmailTaken, ResendTooSoon

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_SEND_FAILED = "We couldn't send the verification email. Please try again later."

_GOOGLE_COOKIE = "yuviz_google_oauth"
_GOOGLE_COOKIE_PATH = "/auth/oauth/google"


def _token_response(user: dict) -> dict:
    return {
        "access_token": create_access_token(user),
        "token_type": "bearer",
        "user": users_service.to_public_dict(user),
    }


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_soon(exc: ResendTooSoon) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail=f"Please wait {exc.retry_after} seconds before requesting another code.",
        headers={"Retry-After": str(exc.retry_after)},
    )


async def _send_code(to_email: str, code: str) -> bool:
    try:
        await email.send_verification_code_email(
            to_email=to_email, code=code, minutes_valid=verification.CODE_TTL_MINUTES,
        )
    except Exception:
        log.exception("verification email to %s failed", to_email)
        return False
    return True


@router.post("/register", status_code=202)
async def register(body: RegisterRequest, request: Request):
    """Emails a code; the account is created only by POST /auth/verify-email.
    Same 202 for a taken email (which gets an "account exists" email instead) so this can't probe accounts."""
    request.app.state.register_throttle.check(_client(request))
    accepted = {"verification_required": True, "email": body.email.lower()}
    try:
        code = await verification.start_registration(**body.model_dump())
    except EmailTaken:
        try:
            await email.send_account_exists_email(to_email=body.email)
        except Exception:
            log.exception("account-exists email to %s failed", body.email)
            raise HTTPException(status_code=503, detail=_SEND_FAILED)
        return accepted
    except ResendTooSoon:
        return accepted
    if not await _send_code(body.email, code):
        await verification.discard_registration(body.email)
        raise HTTPException(status_code=503, detail=_SEND_FAILED)
    return accepted


@router.post("/verify-email")
async def verify_email(body: VerifyEmailRequest, request: Request):
    request.app.state.verify_throttle.check(_client(request))
    try:
        user = await verification.verify_registration(body.email, body.code)
    except CodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except EmailTaken:
        raise HTTPException(status_code=409, detail="that email is already registered — sign in instead")
    return _token_response(user)


@router.post("/resend-code", status_code=202)
async def resend_code(body: ResendCodeRequest, request: Request):
    # Same 202 whether or not a signup is pending, so this can't probe emails.
    request.app.state.verify_throttle.check(_client(request))
    try:
        code = await verification.resend_registration_code(body.email)
    except ResendTooSoon as exc:
        raise _too_soon(exc)
    if code is not None and not await _send_code(body.email, code):
        raise HTTPException(status_code=503, detail=_SEND_FAILED)
    return {"sent": True}


@router.post("/login")
async def login(body: LoginRequest):
    user = await users_service.authenticate(body.email, body.password)
    if user is not None:
        return _token_response(user)
    if await verification.pending_password_matches(body.email, body.password):
        try:
            code = await verification.resend_registration_code(body.email)
        except ResendTooSoon:
            code = None
        if code is not None:
            await _send_code(body.email, code)
        raise HTTPException(
            status_code=403, detail="Please verify your email — enter the code we sent to your inbox.",
        )
    # Same 401 for unknown email and wrong password, so this can't probe accounts.
    raise HTTPException(status_code=401, detail="invalid email or password")


@router.post("/forgot-password", status_code=202)
async def forgot_password(body: ForgotPasswordRequest, request: Request):
    # Same 202 for unknown emails, cooldowns and send failures, so this can't probe accounts.
    request.app.state.verify_throttle.check(_client(request))
    try:
        code = await verification.start_password_reset(body.email)
    except ResendTooSoon:
        code = None
    if code is not None and not await _send_code(body.email, code):
        await verification.discard_password_reset(body.email)
    return {"sent": True}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest, request: Request):
    request.app.state.verify_throttle.check(_client(request))
    try:
        user = await verification.reset_password(body.email, body.code, body.new_password)
    except CodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    forget_user(request.app.state, str(user["id"]))
    return _token_response(user)


def _to_login(**fragment: str) -> RedirectResponse:
    # URL fragment, not query: the browser never sends it to any server or logs.
    return RedirectResponse(f"{google_oauth.ADMIN_UI_URL}/login#{urlencode(fragment)}", status_code=302)


async def _google_user(mode: str, identity: google_oauth.GoogleIdentity) -> dict[str, Any]:
    email = identity.email
    if mode == "create":
        first_name = identity.given_name or email.split("@")[0]
        try:
            # Random password nobody knows: this account signs in with Google only.
            user = await users_service.register_admin(
                email=email,
                password=secrets.token_urlsafe(32),
                organization_name=f"{first_name}'s organization",
                first_name=first_name,
                last_name=identity.family_name,
            )
        except asyncpg.UniqueViolationError:
            raise GoogleOAuthError("That email is already registered — sign in instead.")
        # Google already verified this address; drop any half-finished code signup.
        await verification.discard_registration(email)
        return user
    user = await users_service.get_user_by_email(email)
    if user is None or user.get("is_service_account"):
        raise GoogleOAuthError(f"No account exists for {email}. Create an account first.")
    return user


@router.get("/oauth/google/start")
async def google_start(mode: google_oauth.Mode = "signin"):
    if not google_oauth.enabled():
        return _to_login(error="Google sign-in is not configured on this server.")
    url, nonce = google_oauth.authorization_url(mode)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        _GOOGLE_COOKIE, nonce, max_age=google_oauth.STATE_TTL_SECONDS, path=_GOOGLE_COOKIE_PATH,
        httponly=True, samesite="lax", secure=google_oauth.REDIRECT_URI.startswith("https://"),
    )
    return resp


@router.get("/oauth/google/callback")
async def google_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
):
    try:
        if error or not code or not state:
            raise GoogleOAuthError("Google sign-in was cancelled.")
        mode, nonce = google_oauth.read_state(state, request.cookies.get(_GOOGLE_COOKIE))
        identity = await google_oauth.verified_identity(code, nonce)
        user = await _google_user(mode, identity)
    except GoogleOAuthError as exc:
        resp = _to_login(error=str(exc))
    else:
        resp = _to_login(token=create_access_token(user))
    resp.delete_cookie(_GOOGLE_COOKIE, path=_GOOGLE_COOKIE_PATH)
    return resp


@router.get("/me")
async def me(current_user: CurrentUser = Depends(get_authenticated_user)):
    user = await users_service.get_user_by_id(current_user.id)
    if user is None:
        # 401, not 404, so a deleted user's id doesn't leak existence.
        raise HTTPException(status_code=401, detail="user no longer exists")
    if user["token_version"] != current_user.token_version:
        raise HTTPException(status_code=401, detail="session expired — please sign in again")
    return users_service.to_public_dict(user)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest, request: Request, current_user: CurrentUser = Depends(get_authenticated_user),
):
    """Always the caller's own password. Signs out every other session and
    returns a fresh token for this one."""
    current_user = await fresh_console_authority(request.app.state, current_user)
    user = await users_service.change_password(
        current_user.id,
        current_password=body.current_password,
        new_password=body.new_password,
        platform_scoped=is_platform_scoped(current_user),
    )
    if user is None:
        raise HTTPException(status_code=400, detail="current password is incorrect")
    forget_user(request.app.state, current_user.id)
    return _token_response(user)


@router.post("/change-email", status_code=202)
async def change_email(
    body: ChangeEmailRequest, request: Request, current_user: CurrentUser = Depends(get_authenticated_user),
):
    """Emails a code to the new address; POST /auth/change-email/confirm switches."""
    current_user = await fresh_console_authority(request.app.state, current_user)
    try:
        code = await verification.request_email_change(
            user_id=current_user.id,
            tenant_id=current_user.tenant_id,
            current_password=body.current_password,
            new_email=body.new_email,
        )
    except EmailTaken:
        raise HTTPException(status_code=409, detail="that email is already registered")
    except ResendTooSoon as exc:
        raise _too_soon(exc)
    if code is None:
        raise HTTPException(status_code=400, detail="current password is incorrect")
    if not await _send_code(body.new_email, code):
        await verification.discard_email_change(user_id=current_user.id, tenant_id=current_user.tenant_id)
        raise HTTPException(status_code=503, detail=_SEND_FAILED)
    return {"email": body.new_email.lower()}


@router.post("/change-email/confirm")
async def confirm_email_change(
    body: ConfirmEmailChangeRequest, request: Request, current_user: CurrentUser = Depends(get_authenticated_user),
):
    current_user = await fresh_console_authority(request.app.state, current_user)
    try:
        user = await verification.confirm_email_change(
            user_id=current_user.id, tenant_id=current_user.tenant_id, code=body.code,
        )
    except CodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # The JWT carries the email claim, so hand back a token with the new one.
    return _token_response(user)
