# app/driver/support/support.py

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from typing import Optional, List
from datetime import datetime, timezone
from pathlib import Path
import shutil
import uuid

from app.db.database import get_async_session
from app.db.schema import SupportTicket, SupportStatus, User, UserRole
from app.auth.dependencies import get_current_user  # adjust if your path is different

router = APIRouter(prefix="/support", tags=["Support"])

UPLOAD_DIR = Path("uploads/support")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CREATE SUPPORT TICKET
# ============================================================
@router.post("/create")
async def create_support_ticket(
    subject: str = Form(...),
    description: str = Form(...),
    attachment: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if not subject.strip() or not description.strip():
        raise HTTPException(status_code=400, detail="Subject and description required")

    file_path = None

    if attachment:
        file_ext = attachment.filename.split(".")[-1]
        filename = f"{uuid.uuid4()}.{file_ext}"
        file_location = UPLOAD_DIR / filename

        with open(file_location, "wb") as buffer:
            shutil.copyfileobj(attachment.file, buffer)

        file_path = str(file_location)

    ticket = SupportTicket(
        user_id=current_user.id,
        subject=subject,
        description=description,
        attachment_path=file_path,
        status=SupportStatus.PENDING,
    )

    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)

    return {
        "message": "Support ticket created successfully",
        "ticket_id": ticket.id,
    }


# ============================================================
# GET MY SUPPORT TICKETS
# ============================================================
@router.get("/my-tickets")
async def get_my_tickets(
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(SupportTicket)
        .where(SupportTicket.user_id == current_user.id)
        .order_by(SupportTicket.created_at.desc())
    )

    tickets = result.scalars().all()

    return [
        {
            "id": t.id,
            "subject": t.subject,
            "description": t.description,
            "status": t.status,
            "attachment": t.attachment_path,
            "created_at": t.created_at,
        }
        for t in tickets
    ]


# ============================================================
# ADMIN - GET ALL TICKETS
# ============================================================
@router.get("/admin/all")
async def get_all_tickets(
    status: Optional[SupportStatus] = None,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin only")

    query = select(SupportTicket).order_by(SupportTicket.created_at.desc())

    if status:
        query = query.where(SupportTicket.status == status)

    result = await db.execute(query)
    tickets = result.scalars().all()

    return [
        {
            "id": t.id,
            "user_id": t.user_id,
            "subject": t.subject,
            "description": t.description,
            "status": t.status,
            "attachment": t.attachment_path,
            "created_at": t.created_at,
        }
        for t in tickets
    ]


# ============================================================
# ADMIN - RESOLVE TICKET
# ============================================================
@router.patch("/admin/{ticket_id}/resolve")
async def resolve_ticket(
    ticket_id: str,
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin only")

    result = await db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()

    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    ticket.status = SupportStatus.RESOLVED
    ticket.resolved_by_admin_id = current_user.id
    ticket.resolved_at = datetime.now(timezone.utc)

    await db.commit()

    return {"message": "Ticket resolved successfully"}


# ============================================================
# ADMIN - REJECT TICKET
# ============================================================
@router.patch("/admin/{ticket_id}/reject")
async def reject_ticket(
    ticket_id: str,
    reason: str = Form(...),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin only")

    result = await db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id)
    )
    ticket = result.scalar_one_or_none()

    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    ticket.status = SupportStatus.REJECTED
    ticket.rejection_reason = reason
    ticket.resolved_by_admin_id = current_user.id
    ticket.resolved_at = datetime.now(timezone.utc)

    await db.commit()

    return {"message": "Ticket rejected successfully"}