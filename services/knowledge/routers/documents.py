from __future__ import annotations

import json
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from libs.tenancy import set_target_tenant
from services.config.auth import CurrentUser
from services.config.deps import assert_tenant_access, get_current_user, is_platform_scoped, require_role

from .. import documents as documents_service
from .. import knowledge_bases as kb_service
from ..ingestion_worker import SUPPORTED_CONTENT_TYPES
from ..schemas import DocumentUpdate
from ..storage import LocalStorageProvider

router = APIRouter(tags=["kb_documents"])
_storage = LocalStorageProvider()

MAX_UPLOAD_BYTES = int(os.environ.get("KNOWLEDGE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
_READ_CHUNK_BYTES = 64 * 1024


async def _read_capped(file: UploadFile) -> bytes:
    """Read the upload in chunks, stopping at the first byte over MAX_UPLOAD_BYTES."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_READ_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _authorize_kb(kb_id: str, current_user: CurrentUser) -> dict:
    """A foreign KB gets the same 404 as a missing one."""
    not_found = HTTPException(status_code=404, detail=f"knowledge_base {kb_id!r} not found")
    platform_scoped = is_platform_scoped(current_user)
    kb = await kb_service.get_knowledge_base(kb_id, platform_scoped=platform_scoped)
    if kb is None:
        raise not_found
    try:
        await assert_tenant_access(str(kb["tenant_id"]), current_user)
    except HTTPException:
        raise not_found
    set_target_tenant(str(kb["tenant_id"]))
    return kb


async def _authorize_document(document_id: str, current_user: CurrentUser) -> dict:
    """A foreign document gets the same 404 as a missing one."""
    not_found = HTTPException(status_code=404, detail=f"kb_document {document_id!r} not found")
    platform_scoped = is_platform_scoped(current_user)
    document = await documents_service.get_document(document_id, platform_scoped=platform_scoped)
    if document is None:
        raise not_found
    try:
        await assert_tenant_access(str(document["tenant_id"]), current_user)
    except HTTPException:
        raise not_found
    set_target_tenant(str(document["tenant_id"]))
    return document


@router.get("/knowledge-bases/{kb_id}/documents")
async def list_documents(kb_id: str, current_user: CurrentUser = Depends(get_current_user)):
    await _authorize_kb(kb_id, current_user)
    return await documents_service.list_documents(kb_id)


@router.post("/knowledge-bases/{kb_id}/documents", status_code=201)
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
    title: str = Form(...),
    language: str | None = Form(default=None),
    tags: str = Form(default="{}"),
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    kb = await _authorize_kb(kb_id, current_user)

    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in SUPPORTED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"unsupported content_type {content_type!r} — supported: {sorted(SUPPORTED_CONTENT_TYPES)}",
        )
    content = await _read_capped(file)
    return await documents_service.upload_document(
        tenant_id=kb["tenant_id"],
        kb_id=kb_id,
        title=title,
        filename=file.filename or "document",
        content_type=content_type,
        content=content,
        storage=_storage,
        language=language,
        tags=json.loads(tags),
        user_id=current_user.id,
        user_email=current_user.email,
    )


@router.get("/documents/{document_id}")
async def get_document(document_id: str, current_user: CurrentUser = Depends(get_current_user)):
    return await _authorize_document(document_id, current_user)


@router.patch("/documents/{document_id}")
async def update_document(
    document_id: str,
    body: DocumentUpdate,
    current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    document = await _authorize_document(document_id, current_user)
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="request body has no fields to update")
    platform_scoped = is_platform_scoped(current_user)
    return await documents_service.update_document(
        document_id,
        platform_scoped=platform_scoped,
        stamp_tenant=str(document["tenant_id"]) if platform_scoped else None,
        user_id=current_user.id, user_email=current_user.email, **fields,
    )


@router.post("/documents/{document_id}/retry")
async def retry_document(
    document_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    document = await _authorize_document(document_id, current_user)
    platform_scoped = is_platform_scoped(current_user)
    retried = await documents_service.retry_document(
        document_id,
        tenant_id=document["tenant_id"],
        platform_scoped=platform_scoped,
        stamp_tenant=str(document["tenant_id"]) if platform_scoped else None,
        user_id=current_user.id, user_email=current_user.email,
    )
    if retried is None:
        raise HTTPException(status_code=409, detail="only a failed document can be retried")
    return retried


@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(
    document_id: str, current_user: CurrentUser = Depends(require_role("superadmin", "admin")),
):
    document = await _authorize_document(document_id, current_user)
    platform_scoped = is_platform_scoped(current_user)
    await documents_service.soft_delete_document(
        document_id,
        platform_scoped=platform_scoped,
        stamp_tenant=str(document["tenant_id"]) if platform_scoped else None,
        user_id=current_user.id, user_email=current_user.email,
    )
