"""Chat session CRUD - /api/chat/*. Ported from backend/chat_routes.py.

Note (carried over from the legacy code, not introduced here): this blueprint has
no authentication/ownership check - any caller can list/read/rename/delete any
session by numeric ID. Kept as-is to match the original behavior; add a
session-ownership check here if that turns out to matter for this deployment.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.models.chat_session import ChatSession
from app.db.session import get_db

router = APIRouter(prefix="/chat", tags=["chat"])

MAX_SESSIONS_RETURNED = 50


class SessionCreateRequest(BaseModel):
    title: Optional[str] = None


class SessionUpdateRequest(BaseModel):
    title: str


def _client_ip(request: Request) -> Optional[str]:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db)):
    sessions = (
        db.query(ChatSession)
        .order_by(ChatSession.updated_at.desc())
        .limit(MAX_SESSIONS_RETURNED)
        .all()
    )
    return {"sessions": [s.to_dict() for s in sessions]}


@router.post("/sessions", status_code=201)
def create_session(payload: SessionCreateRequest, request: Request, db: Session = Depends(get_db)):
    title = (payload.title or "Percakapan Baru").strip()[:120] or "Percakapan Baru"
    session = ChatSession(title=title, ip_address=_client_ip(request))
    db.add(session)
    db.commit()
    db.refresh(session)
    return session.to_dict()


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)):
    session = db.get(ChatSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict(include_messages=True)


@router.put("/sessions/{session_id}")
def update_session(session_id: int, payload: SessionUpdateRequest, db: Session = Depends(get_db)):
    session = db.get(ChatSession, session_id)
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
def delete_session(session_id: int, db: Session = Depends(get_db)):
    session = db.get(ChatSession, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete(session)
    db.commit()
    return {"ok": True}
