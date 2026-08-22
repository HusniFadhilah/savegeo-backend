from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SystemConfig(Base):
    __tablename__ = "system_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    value: Mapped[str | None] = mapped_column(Text)
    value_type: Mapped[str] = mapped_column(String(16), default="string")  # string|int|float|bool|json
    category: Mapped[str] = mapped_column(String(64), default="general")
    label: Mapped[str | None] = mapped_column(String(256))
    description: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), onupdate=lambda: dt.datetime.now(dt.UTC)
    )
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id"))

    def typed_value(self):
        if self.value is None:
            return None
        if self.value_type == "int":
            return int(self.value)
        if self.value_type == "float":
            return float(self.value)
        if self.value_type == "bool":
            return self.value.lower() in ("true", "1", "yes")
        if self.value_type == "json":
            return json.loads(self.value)
        return self.value

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.typed_value(),
            "raw_value": self.value,
            "value_type": self.value_type,
            "category": self.category,
            "label": self.label,
            "description": self.description,
            "is_public": self.is_public,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
