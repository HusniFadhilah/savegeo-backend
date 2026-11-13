"""MFA secrets encrypted outside the DB keyring; opaque challenges stored hashed."""
import datetime as dt

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AdminMfa(Base):
    __tablename__ = "admin_mfa"
    admin_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"), primary_key=True)
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    recovery_hashes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    last_counter: Mapped[int] = mapped_column(BigInteger, nullable=False, default=-1)
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    enrolled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class MfaChallenge(Base):
    __tablename__ = "mfa_challenges"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    password_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    pending_secret: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
