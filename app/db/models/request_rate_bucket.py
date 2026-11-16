"""Expiring aggregate counters; no raw account or IP identifiers are stored."""
from sqlalchemy import BigInteger, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class RequestRateBucket(Base):
    __tablename__ = "request_rate_buckets"
    __table_args__ = (Index("ix_request_rate_buckets_expires_at", "expires_at"),)
    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
