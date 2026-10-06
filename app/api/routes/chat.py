"""Chat session CRUD - /api/chat/*. Ported from backend/chat_routes.py.

Chat history is private to the authenticated app viewer. Legacy rows without an
owner remain stored but are not exposed because their owner cannot be safely
inferred from the old IP-only metadata.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.chat_ownership import chat_session_owner_key
from app.core.security import get_current_app_viewer
from app.db.models.admin_user import AdminUser
from app.db.models.chat_session import ChatSession
from app.db.session import get_db
from app.db.models.user import User

router = APIRouter(prefix="/chat", tags=["chat"])

MAX_SESSIONS_RETURNED = 50


class SessionCreateRequest(BaseModel):
    title: str | None = None


class SessionUpdateRequest(BaseModel):
    title: str


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/sessions")
def list_sessions(
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    sessions = (
        db.query(ChatSession)
        .filter(ChatSession.owner_key == chat_session_owner_key(viewer))
        .order_by(ChatSession.updated_at.desc())
        .limit(MAX_SESSIONS_RETURNED)
        .all()
    )
    return {"sessions": [s.to_dict() for s in sessions]}


@router.post("/sessions", status_code=201)
def create_session(
    payload: SessionCreateRequest,
    request: Request,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    title = (payload.title or "Percakapan Baru").strip()[:120] or "Percakapan Baru"
    session = ChatSession(
        title=title,
        ip_address=_client_ip(request),
        owner_key=chat_session_owner_key(viewer),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session.to_dict()


@router.get("/sessions/{session_id}")
def get_session(
    session_id: int,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    session = _owned_session(db, session_id, viewer)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict(include_messages=True)


@router.put("/sessions/{session_id}")
def update_session(
    session_id: int,
    payload: SessionUpdateRequest,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    session = _owned_session(db, session_id, viewer)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    title = payload.title.strip()[:120]
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    session.title = title
    db.commit()
    db.refresh(session)
    return session.to_dict()


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: int,
    viewer: User | AdminUser = Depends(get_current_app_viewer),
    db: Session = Depends(get_db),
):
    session = _owned_session(db, session_id, viewer)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete(session)
    db.commit()
    return {"ok": True}


def _owned_session(db: Session, session_id: int, viewer: User | AdminUser) -> ChatSession | None:
    return (
        db.query(ChatSession)
        .filter(
            ChatSession.id == session_id,
            ChatSession.owner_key == chat_session_owner_key(viewer),
        )
        .first()
    )
