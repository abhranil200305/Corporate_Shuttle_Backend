# app/system_user.py
from __future__ import annotations

import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.schema import User, UserRole


DEFAULT_SYSTEM_FINE_REGISTER_EMAIL = "system-fine-register@internal.invalid"


def get_system_fine_register_email() -> str:
    value = os.getenv(
        "SYSTEM_FINE_REGISTER_EMAIL",
        DEFAULT_SYSTEM_FINE_REGISTER_EMAIL,
    ).strip().lower()

    if not value:
        raise RuntimeError("SYSTEM_FINE_REGISTER_EMAIL cannot be empty.")

    return value


async def ensure_system_fine_register_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> User:
    system_email = get_system_fine_register_email()

    async with session_factory() as session:
        stmt = select(User).where(User.email == system_email).limit(1)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if user is None:
            user = User(
                email=system_email,
                role=UserRole.ADMIN,
                is_active=False,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user

        changed = False

        if user.role != UserRole.ADMIN:
            user.role = UserRole.ADMIN
            changed = True

        if user.is_active:
            user.is_active = False
            changed = True

        if changed:
            session.add(user)
            await session.commit()
            await session.refresh(user)

        return user


async def get_system_fine_register_user_id(
    session: AsyncSession,
) -> str:
    system_email = get_system_fine_register_email()

    stmt = select(User.id).where(User.email == system_email).limit(1)
    result = await session.execute(stmt)
    user_id = result.scalar_one_or_none()

    if user_id is None:
        raise RuntimeError(
            "System fine register user is missing. "
            "Run startup bootstrap before using fine registration."
        )

    return user_id