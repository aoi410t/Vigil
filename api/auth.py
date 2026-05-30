"""Multi-static auth context (v1.6.0; dev/user mode split v1.7.1).

The HTTP Basic middleware in `api/main.py` validates the password (either
`AUTH_PASSWORD` for user mode or `DEV_PASSWORD` for dev mode) and stashes
which one matched on `request.state.auth_match`. This module reads that
signal to set `User.is_developer` on first sighting / refresh it on
subsequent logins.

What's different between dev and user mode (v1.7.1):
- Dev users see the **Abilities review queue**, **Field data panel**,
  **"show all encounters"** toggle, and the **Default Static** (id=1)
  which holds pre-1.6.0 dev-ingested data.
- User-mode users see a curated subset focused on their static's own
  pulls — they get their OWN static auto-created on first login (named
  "{username}'s raid"), separate from Default Static.

Authorization is membership-based: a user can see/modify data only for
statics they're a member of. `require_static_membership` raises 404 (not
403) when the requested static_id isn't in the user's membership set —
avoids leaking existence.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import settings
from db.models import Static, StaticMembership, User
from db.session import SessionLocal

DEFAULT_STATIC_ID = 1
DEFAULT_STATIC_NAME = "Default Static"
DEV_FALLBACK_USERNAME = "dev"


@dataclass(frozen=True)
class Context:
    """Per-request auth context. Filled in by `get_context()` dependency."""

    user_id: int
    username: str
    current_static_id: int
    is_developer: bool


def _extract_username_from_request(request: Request) -> Optional[str]:
    """Decode HTTP Basic header and return the username, or None if absent."""
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(h.split(" ", 1)[1]).decode("utf-8")
        username, _, _ = decoded.partition(":")
        return username or None
    except (ValueError, UnicodeDecodeError):
        return None


def _ensure_default_static(session: Session) -> int:
    """Idempotent: ensure the Default Static row exists. Returns its id."""
    s = session.get(Static, DEFAULT_STATIC_ID)
    if s is not None:
        return s.id
    s = Static(id=DEFAULT_STATIC_ID, name=DEFAULT_STATIC_NAME,
               created_at=datetime.now(timezone.utc))
    session.add(s)
    session.flush()
    return s.id


def _resolve_is_developer(request: Request, username: str) -> bool:
    """Determine whether this request is in dev mode.

    Priority:
    1. Middleware set `request.state.auth_match`:
       - "dev"  → DEV_PASSWORD matched → True
       - "user" → AUTH_PASSWORD matched → False
    2. No middleware signal (no auth env configured / dev-test mode):
       - Backwards-compat: True if username == AUTH_USERNAME (when set).
       - Otherwise False.
    """
    match = getattr(request.state, "auth_match", None)
    if match == "dev":
        return True
    if match == "user":
        return False
    # No auth configured — fallback for dev/test convenience.
    if settings.auth_username and username == settings.auth_username:
        return True
    return False


def _create_user_static(session: Session, user: User) -> int:
    """Create a fresh static named after the user. Returns its id.

    Does NOT add the membership — caller handles that so we don't
    double-insert.
    """
    name = f"{user.username}'s raid"
    s = Static(name=name, created_at=datetime.now(timezone.utc))
    session.add(s)
    session.flush()
    return s.id


def ensure_user_and_membership(
    session: Session, username: str, *, is_developer: bool,
) -> User:
    """Idempotent: ensure a `users` row exists for `username`, has the
    correct `is_developer` flag, and is a member of at least one static.

    Dev users join the Default Static if they aren't a member of any.
    Non-dev users get their own static auto-created if they aren't a
    member of any.
    """
    user = session.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()

    if user is None:
        user = User(
            username=username,
            is_developer=is_developer,
            created_at=datetime.now(timezone.utc),
        )
        session.add(user)
        session.flush()
    else:
        # Refresh is_developer on every login so a password rotation flips
        # the flag without manual DB poking.
        if user.is_developer != is_developer:
            user.is_developer = is_developer
            session.flush()

    has_membership = session.execute(
        select(StaticMembership)
        .where(StaticMembership.user_id == user.id)
    ).first()
    if not has_membership:
        if is_developer:
            target_id = _ensure_default_static(session)
        else:
            target_id = _create_user_static(session, user)
        session.add(StaticMembership(
            user_id=user.id, static_id=target_id,
            joined_at=datetime.now(timezone.utc),
        ))
        session.flush()
        if user.current_static_id is None:
            user.current_static_id = target_id
            session.flush()
    elif user.current_static_id is None:
        # Membership exists but no current — pick the first one.
        first = session.execute(
            select(StaticMembership)
            .where(StaticMembership.user_id == user.id)
            .limit(1)
        ).scalar_one()
        user.current_static_id = first.static_id
        session.flush()
    return user


def get_context(request: Request) -> Context:
    """FastAPI dependency: per-request auth context.

    Username comes from the HTTP Basic header; dev/user mode comes from
    which password matched (stashed by the middleware on request.state).
    """
    username = _extract_username_from_request(request)
    if username is None:
        username = settings.auth_username or DEV_FALLBACK_USERNAME
    is_dev = _resolve_is_developer(request, username)
    with SessionLocal() as session:
        user = ensure_user_and_membership(session, username,
                                          is_developer=is_dev)
        session.commit()
        session.refresh(user)
        if user.current_static_id is None:
            raise HTTPException(status_code=500,
                                detail="user has no current static")
        return Context(
            user_id=user.id,
            username=user.username,
            current_static_id=user.current_static_id,
            is_developer=user.is_developer,
        )


def require_static_membership(session: Session, user_id: int,
                              static_id: int) -> None:
    """Raise 404 if user is not a member of static_id. 404 instead of 403
    to avoid leaking existence of statics the user can't see."""
    m = session.execute(
        select(StaticMembership)
        .where(StaticMembership.user_id == user_id,
               StaticMembership.static_id == static_id)
    ).first()
    if not m:
        raise HTTPException(status_code=404, detail="static not found")
