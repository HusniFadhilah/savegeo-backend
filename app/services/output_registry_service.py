"""Persistence and normalization for geospatial action outputs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.temporal import format_rfc3339
from app.db.models.analysis_output import AnalysisOutput
from app.db.models.analysis_provenance import AnalysisProvenance


def _stable_id(action_id: str, suffix: str) -> str:
    return f"{action_id}:{suffix}"[:128]


def _checksum(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def register_output(
    db: Session,
    action_id: str,
    *,
    output_id: str,
    title: str,
    role: str,
    media_type: str,
    href: str,
    profile: str | None = None,
    metadata: dict[str, Any] | None = None,
    status: str = "completed",
    checksum: str | None = None,
    feature_count: int | None = None,
) -> AnalysisOutput:
    existing = db.get(AnalysisOutput, output_id)
    if existing is not None:
        return existing
    output = AnalysisOutput(
        id=output_id,
        action_id=action_id,
        title=title,
        role=role,
        media_type=media_type,
        profile=profile,
        href=href,
        metadata_json=metadata or {},
        status=status,
        checksum=checksum,
        feature_count=feature_count,
        created_at=datetime.now(UTC),
    )
    db.add(output)
    db.flush()
    return output


def register_action_outputs(db: Session, action_id: str, result: Any = None) -> list[AnalysisOutput]:
    """Register safe descriptors without storing large raster/vector payloads."""
    outputs = [
        register_output(
            db,
            action_id,
            output_id=_stable_id(action_id, "provenance"),
            title="Action provenance",
            role="provenance",
            media_type="application/json",
            href=f"/api/actions/{action_id}/outputs/provenance/metadata",
            metadata={"action_id": action_id},
            status="completed" if result is not None else "queued",
        )
    ]
    if not isinstance(result, dict):
        return outputs

    geojson = result.get("geojson") or result.get("result_geojson")
    if geojson is None and isinstance(result.get("features"), list):
        geojson = {"type": "FeatureCollection", "features": result["features"]}
    if isinstance(geojson, dict) and geojson.get("type") in {"Feature", "FeatureCollection"}:
        features = geojson.get("features", [geojson]) if geojson.get("type") == "FeatureCollection" else [geojson]
        outputs.append(
            register_output(
                db,
                action_id,
                output_id=_stable_id(action_id, "geojson"),
                title="GeoJSON result",
                role="data",
                media_type="application/geo+json",
                href=f"/api/actions/{action_id}/outputs/geojson/content",
                profile="rfc-7946",
                metadata={"crs": "EPSG:4326", "coordinate_order": "longitude,latitude"},
                checksum=_checksum(geojson),
                feature_count=len(features),
            )
        )

    tilejson = result.get("tilejson")
    if isinstance(tilejson, dict) and tilejson.get("tilejson") == "3.0.0":
        outputs.append(
            register_output(
                db,
                action_id,
                output_id=_stable_id(action_id, "tilejson"),
                title="TileJSON metadata",
                role="visualization",
                media_type="application/json",
                profile="tilejson-3.0.0",
                href=f"/api/actions/{action_id}/outputs/tilejson/content",
                metadata=tilejson,
                checksum=_checksum(tilejson),
            )
        )
    db.commit()
    return outputs


def get_action(db: Session, action_id: str) -> dict[str, Any] | None:
    action = db.get(AnalysisProvenance, action_id)
    if action is None:
        return None
    outputs = list(
        db.scalars(select(AnalysisOutput).where(AnalysisOutput.action_id == action_id).order_by(AnalysisOutput.created_at))
    )
    return {
        "action_id": action.id,
        "action_type": action.analysis_type,
        "status": action.status,
        "created_at": format_rfc3339(action.created_at, field="created_at"),
        "completed_at": format_rfc3339(action.completed_at, field="completed_at") if action.completed_at else None,
        "outputs": [item.to_dict() for item in outputs],
        "provenance": action.to_dict(),
    }
