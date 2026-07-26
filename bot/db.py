from datetime import UTC, datetime
from typing import Annotated

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from bot.config import get_settings


class Base(DeclarativeBase):
    """DeclarativeBase для всех моделей (SQLAlchemy 2.0)."""


settings = get_settings()

engine = create_async_engine(settings.DATABASE_URL, future=True)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def create_all() -> None:
    """Создать все таблицы (для dev/test). В проде — alembic upgrade head."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all() -> None:
    """Drop all tables (для тестов)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def dispose() -> None:
    await engine.dispose()


# Type alias для удобства аннотаций в handlers/services
SessionDep = Annotated[AsyncSession, "session"]


# Утилита для тестов и on_startup
def utcnow() -> datetime:
    """Replaces deprecated datetime.utcnow() (PEP 587, Python 3.12)."""
    return datetime.now(UTC)
