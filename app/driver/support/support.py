# app/driver/support/support.py

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Header
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import Optional
from datetime import datetime, timezone
import shutil
import uuid
from pathlib import Path as FSPath
from fastapi import Header
from app.notifications.service import NotificationService
from app.notifications.hub import WSHub
from fastapi import Request  


from app.db.database import get_async_session
from app.db.schema import SupportTicket, SupportStatus, User, UserRole
from app.auth.dependencies import get_current_user  

router = APIRouter(prefix="/support", tags=["Support"])

# File upload directory
UPLOAD_DIR = FSPath("uploads/support")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# CREATE SUPPORT TICKET
# ============================================================


@router.post("/create")
async def create_support_ticket(
    request: Request,  # ✅ IMPORTANT FIX
    subject: str = Form(...),
    description: str = Form(...),
    attachment: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    # ===========================
    # VALIDATION
    # ===========================
    if not subject.strip() or not description.strip():
        raise HTTPException(status_code=400, detail="Subject and description required")

    # ===========================
    # FILE UPLOAD
    # ===========================
    file_path = None
    if attachment and attachment.filename:
        file_ext = attachment.filename.split(".")[-1]
        filename = f"{uuid.uuid4()}.{file_ext}"
        file_location = UPLOAD_DIR / filename

        with open(file_location, "wb") as buffer:
            shutil.copyfileobj(attachment.file, buffer)

        file_path = str(file_location)

    # ===========================
    # CREATE TICKET
    # ===========================
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

    # ===========================
    # SEND NOTIFICATION TO ADMINS
    # ===========================
    result = await db.execute(
        select(User).where(User.role == UserRole.ADMIN)
    )
    admins = result.scalars().all()

    # ✅ CORRECT WAY to get ws_hub
    ws_hub: WSHub | None = getattr(request.app.state, "ws_hub", None)

    notification_service = NotificationService(
        db=db,
        ws_hub=ws_hub
    )

    # ===========================
    # NOTIFY EACH ADMIN
    # ===========================
    for admin in admins:
        await notification_service.notify_user(
            user_id=admin.id,
            title="New Support Ticket",
            message=f"A new support ticket has been submitted by {current_user.email}",
            data={
                "ticket_id": ticket.id,
                "subject": subject,
                "description": description,
            },
        )

    # ===========================
    # RESPONSE
    # ===========================
    return {
        "message": "Support ticket created successfully",
        "ticket_id": ticket.id
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
# DRIVER - GET SINGLE SUPPORT TICKET (ticket_id from header)
# ============================================================

@router.get("/driver/view")
async def driver_view_support_ticket(
    ticket_id: Optional[str] = Header(None, description="Support ticket ID"),
    db: AsyncSession = Depends(get_async_session),
    current_user: User = Depends(get_current_user),
):
    """
    If ticket_id is provided → return that ticket
    If not → return ALL tickets of the user
    """

    # ============================
    # CASE 1: Specific ticket
    # ============================
    if ticket_id:
        result = await db.execute(
            select(SupportTicket).where(SupportTicket.id == ticket_id)
        )
        ticket = result.scalar_one_or_none()

        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        if ticket.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized")

        return {
            "id": ticket.id,
            "subject": ticket.subject,
            "description": ticket.description,
            "status": ticket.status,
            "attachment": ticket.attachment_path,
            "created_at": ticket.created_at,
            "resolved_at": ticket.resolved_at,
            "rejection_reason": ticket.rejection_reason,
        }

    # ============================
    # CASE 2: All tickets
    # ============================
    result = await db.execute(
        select(SupportTicket)
        .where(SupportTicket.user_id == current_user.id)
        .order_by(SupportTicket.created_at.desc())
    )

    tickets = result.scalars().all()

    if not tickets:
        raise HTTPException(status_code=204, detail="No support tickets found")

    return [
        {
            "id": t.id,
            "subject": t.subject,
            "description": t.description,
            "status": t.status,
            "attachment": t.attachment_path,
            "created_at": t.created_at,
            "resolved_at": t.resolved_at,
            "rejection_reason": t.rejection_reason,
        }
        for t in tickets
    ]