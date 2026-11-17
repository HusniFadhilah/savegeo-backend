"""Short-lived public provider responses, shared by every API process."""
from sqlalchemy import Float, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class ProviderCache(Base):
    __tablename__ = "provider_cache"
    __table_args__ = (
        Index("ix_provider_cache_expires_at", "expires_at"),
        Index("ix_provider_cache_provider_expiry", "provider", "expires_at"),
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[dict | list | None] = mapped_column(JSON)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    next_request_at: Mapped[float] = mapped_column(Float, nullable=False, default=0)
