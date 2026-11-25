"""Shared authorization dependencies; API routers never import the ASGI app."""

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEvent, AuthSession, Project, User
from app.security import SESSION_COOKIE, aware_utc, token_hash


def get_db(request: Request):
    with request.app.state.sessions() as db:
        yield db


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 128:
        raise HTTPException(401, "Authentication required")
    digest = token_hash(token, request.app.state.settings.session_secret.get_secret_value())
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest))
    if (
        not session
        or session.revoked_at is not None
        or aware_utc(session.expires_at) <= datetime.now(UTC)
    ):
        raise HTTPException(401, "Session expired or invalid")
    user = db.get(User, session.user_id)
    if not user or not user.active:
        raise HTTPException(401, "Session expired or invalid")
    request.state.auth_session = session
    return user


def editor(user: User = Depends(current_user)) -> User:
    if user.role not in {"editor", "admin"}:
        raise HTTPException(403, "Editor role required")
    return user


def administrator(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Administrator role required")
    return user


def audit(db: Session, kind: str, user: User | None = None, project: Project | None = None):
    db.add(
        AuditEvent(
            event_type=kind,
            actor_user_id=user.id if user else None,
            project_id=project.id if project else None,
            entity_type="project" if project else "authentication",
            entity_id=project.id if project else None,
            details={},
        )
    )
