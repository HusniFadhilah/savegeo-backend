from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class GEECredential(Base):
    """Metadata for a GEE service-account key. The raw JSON key file itself lives in a
    private Supabase Storage bucket (see app/services/storage_service.py) — only the
    bucket object path is stored here, never the private key material.
    """

    __tablename__ = "gee_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    client_email: Mapped[str] = mapped_column(String(256), nullable=False)
    json_filename: Mapped[str] = mapped_column(String(256), nullable=False)
    bucket_path: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("admin_users.id"))
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc))
    last_used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    uploader: Mapped["AdminUser | None"] = relationship("AdminUser", backref="gee_credentials")

    def to_dict(self, include_path: bool = False) -> dict:
        data = {
            "id": self.id,
            "label": self.label,
            "project_id": self.project_id,
            "client_email": self.client_email,
            "json_filename": self.json_filename,
            "is_active": self.is_active,
            "uploaded_by": self.uploader.username if self.uploader else None,
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "notes": self.notes,
        }
        if include_path:
            data["bucket_path"] = self.bucket_path
        return data
