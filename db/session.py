"""Database engine/session factory. DATABASE_URL is read from env (.env)."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from api.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models (PLAN.md §6 tables land here in T-003)."""


def _make_engine():
    if not settings.database_url:
        return None
    return create_engine(settings.database_url, future=True, pool_pre_ping=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True) if engine else None
