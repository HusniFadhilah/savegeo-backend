from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import Boolean, DateTime, Float, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CompanyBoundary(Base):
    __tablename__ = "company_boundaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(255))
    industry_type: Mapped[str] = mapped_column(String(50), nullable=False)  # mining|forestry|plantation|energy
    sub_type: Mapped[str | None] = mapped_column(String(100))
    province: Mapped[str | None] = mapped_column(String(100))
    district: Mapped[str | None] = mapped_column(String(100))
    # Native JSONB (legacy stored this as a raw Text JSON string).
    geojson: Mapped[dict] = mapped_column(JSONB, nullable=False)
    area_ha: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(50), default="manual")  # manual|osm|gfw
    source_url: Mapped[str | None] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc), onupdate=lambda: dt.datetime.now(dt.timezone.utc)
    )

    def to_dict(self, include_geojson: bool = False) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "company_name": self.company_name,
            "industry_type": self.industry_type,
            "sub_type": self.sub_type,
            "province": self.province,
            "district": self.district,
            "area_ha": self.area_ha,
            "source": self.source,
            "source_url": self.source_url,
            "description": self.description,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_geojson:
            data["geojson"] = self.geojson
        return data
