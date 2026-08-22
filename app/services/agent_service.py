"""Agentic AI service - capabilities document, plan+analyze, and AI-controller chat.

Ported (business logic unchanged) from legacy `backend/app.py`:
  - _agent_capabilities_document: lines ~1739-1758
  - agent_analyze route body: lines ~1767-1809
  - agent_control route body: lines ~1812-1950

The legacy `agent_analyze` executed whitelisted tool calls via
`app.test_client()` (Flask's in-process test client). The FastAPI equivalent
is an `httpx.Client` bound to the app via `httpx.ASGITransport` - see
`run_agent_analyze` below. `app.main` is imported lazily inside the function
body (not at module level) to avoid a circular import, since `app.main`
imports the routers which import this service module.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from app.agentic import rate_limiter
from app.agentic.agentic_ai import (
    build_agent_capabilities,
    execute_agent_plan,
    geoai_with_ai,
    plan_agent_request,
    plan_with_ai,
)
from app.db.models.chat_message import ChatMessage
from app.db.models.chat_session import ChatSession
from app.db.models.uploaded_model import UploadedModel
from app.providers.arcgis_client import get_arcgis_client
from app.registries.landcover_dataset_registry import get_dataset_list
from app.services.carbon_service import get_carbon_dataset_list

logger = logging.getLogger(__name__)


def build_capabilities_document(db: Session, request: Request) -> dict:
    """Build capabilities consumed by the agent planner and frontend."""
    carbon_datasets = get_carbon_dataset_list(db, require_model=True)
    landcover_datasets = get_dataset_list(module="landcover")
    models = [
        model.to_dict_public(include_path=False)
        for model in db.query(UploadedModel).filter_by(model_type="carbon", is_active=True).all()
    ]
    arcgis_client = get_arcgis_client()
    return build_agent_capabilities(
        carbon_datasets=carbon_datasets,
        landcover_datasets=landcover_datasets,
        models=models,
        ee_initialized=bool(getattr(request.app.state, "ee_initialized", False)),
        arcgis_enabled=bool(arcgis_client.is_enabled()),
    )


def run_agent_analyze(db: Session, request: Request, payload: dict) -> dict:
    """
    Agentic AI entrypoint.

    Body:
      {
        "message": "Analisis karbon AOI ini pakai GEDI",
        "aoi": {...},
        "execute": false
      }

    By default the agent returns a validated plan. Set execute=true to run the
    whitelisted GeoMoka analysis endpoint selected by the planner.
    """
    capabilities = build_capabilities_document(db, request)
    plan = plan_agent_request(payload, capabilities)
    response: dict[str, Any] = {
        "agent": capabilities["agent"],
        "plan": plan,
        "capability_snapshot": {
            "carbon_dataset_count": len(capabilities["datasets"]["carbon"]),
            "landcover_dataset_count": len(capabilities["datasets"]["landcover"]),
            "carbon_model_count": len(capabilities["models"]["carbon"]),
            "earth_engine_initialized": capabilities["runtime"]["earth_engine_initialized"],
            "arcgis_enabled": capabilities["runtime"]["arcgis_enabled"],
        },
    }

    execute = bool(payload.get("execute", False))
    if execute:
        import httpx

        from app.main import app as fastapi_app

        transport = httpx.ASGITransport(app=fastapi_app)
        with httpx.Client(transport=transport, base_url="http://internal") as client:
            response["execution"] = execute_agent_plan(plan, client)
    else:
        response["execution"] = {
            "executed": False,
            "reason": "Set execute=true untuk menjalankan workflow yang sudah direncanakan.",
        }

    return response


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def run_agent_control(db: Session, request: Request, payload: dict) -> tuple[dict, int, dict]:
    """
    AI Controller endpoint.
    Body: { message, page_state, image?, attachment?, session_id? }
    Returns: (result_dict, status_code, extra_headers) - matching legacy's varied
    status codes (429 rate limit, 400 validation, 500 provider error, 200 ok),
    which don't all fit the plain `{"error": ...}` shape our global exception
    handler produces (the rate-limit body in particular carries its own
    intent/message/actions/retry_after shape).
    """
    # ── Rate limiting ────────────────────────────────────────────
    client_ip = _client_ip(request)
    allowed, rl_msg, retry_after = rate_limiter.check(client_ip)
    if not allowed:
        return (
            {
                "intent": "rate_limited",
                "confidence": 0.0,
                "needs_confirmation": False,
                "message": rl_msg,
                "warnings": [],
                "actions": [],
                "retry_after": retry_after,
            },
            429,
            {"Retry-After": str(retry_after)},
        )

    # ── Parse payload ────────────────────────────────────────────
    mode = str(payload.get("mode") or "control").strip().lower()
    message = str(payload.get("message") or "").strip()
    page_state = payload.get("page_state") or {}
    geoai_context = payload.get("context") or {}
    image_b64 = payload.get("image") or None
    attachment = payload.get("attachment") or None
    session_id = payload.get("session_id") or None

    limits = rate_limiter.get_limits()
    max_chars = limits["max_chars"]

    if len(message) > max_chars:
        return (
            {"error": f"Pesan terlalu panjang ({len(message)} karakter, maks {max_chars})."},
            400,
            {},
        )

    if attachment:
        b64_data = attachment.get("b64") or ""
        estimated_mb = len(b64_data) * 0.75 / (1024 * 1024)
        max_mb = limits["max_file_mb"]
        if estimated_mb > max_mb:
            return (
                {"error": f"File terlalu besar ({estimated_mb:.1f} MB, maks {max_mb} MB)."},
                400,
                {},
            )

    if not message and not image_b64 and not attachment:
        return {"error": "message is required"}, 400, {}

    # ── Session management ───────────────────────────────────────
    session: ChatSession | None = None
    if session_id:
        session = db.get(ChatSession, session_id)

    # Build history from last 10 messages in this session
    history: list = []
    if session:
        past = session.messages[-10:]
        history = [{"role": m.role, "content": m.content} for m in past]

    # ── AI call ──────────────────────────────────────────────────
    try:
        if mode == "geoai":
            # SaveGeo Assistant: grounded tool-calling loop over already-computed
            # results + live hotspot search (see agentic_ai.geoai_with_ai). Screenshot/
            # attachment inputs are UI-automation-mode-only and not passed through here.
            result = geoai_with_ai(message, geoai_context, db, history)
        else:
            result = plan_with_ai(message, page_state, image_b64, attachment, history)
    except Exception as e:  # noqa: BLE001
        logger.error("agent_control error: %s", e)
        err_str = str(e)
        if any(code in err_str for code in ("503", "UNAVAILABLE", "overloaded", "529")):
            user_msg = "Server AI sedang sibuk. Coba lagi dalam beberapa detik."
        elif "429" in err_str or "quota" in err_str.lower():
            user_msg = "Batas kuota AI tercapai. Coba lagi nanti atau ganti model/provider di admin."
        elif "401" in err_str or "403" in err_str or "API key" in err_str:
            user_msg = "API key tidak valid atau belum dikonfigurasi. Cek pengaturan admin."
        else:
            user_msg = "Terjadi kesalahan pada AI. Coba lagi beberapa saat."
        return (
            {
                "intent": "error",
                "confidence": 0.0,
                "needs_confirmation": False,
                "message": user_msg,
                "warnings": [err_str],
                "actions": [],
            },
            500,
            {},
        )

    # ── Persist messages ─────────────────────────────────────────
    try:
        if session is None:
            # Auto-create session titled from first message
            title = (message or "Percakapan Baru")[:80]
            session = ChatSession(title=title, ip_address=client_ip)
            db.add(session)
            db.flush()  # get session.id without committing

        # User message
        user_label = message or ("[Screenshot]" if image_b64 else "[File]")
        if attachment and attachment.get("name"):
            user_label = (user_label + f" [{attachment['name']}]").strip()
        db.add(ChatMessage(
            session_id=session.id,
            role="user",
            content=user_label,
            has_image=bool(image_b64),
            file_name=attachment.get("name") if attachment else None,
        ))

        # Assistant message — store full response JSON so history can replay
        # message text + action steps + warnings.
        import json as _json

        assistant_full = _json.dumps({
            "message": result.get("message", ""),
            "actions": result.get("actions", []),
            "warnings": result.get("warnings", []),
            "cards": result.get("cards", []),
        }, ensure_ascii=False)
        db.add(ChatMessage(
            session_id=session.id,
            role="assistant",
            content=assistant_full,
        ))

        session.updated_at = datetime.now(UTC)
        db.commit()
        result["session_id"] = session.id
    except Exception as e:  # noqa: BLE001
        logger.warning("chat history save failed: %s", e)
        db.rollback()

    return result, 200, {}
