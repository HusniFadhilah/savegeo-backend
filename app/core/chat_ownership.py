"""Stable ownership keys for persisted chatbot conversations."""

from app.db.models.admin_user import AdminUser
from app.db.models.user import User


def chat_session_owner_key(viewer: AdminUser | User) -> str:
    """Namespace numeric IDs so user and admin accounts never collide."""
    if isinstance(viewer, AdminUser):
        return f"admin:{viewer.id}"
    if isinstance(viewer, User):
        return f"user:{viewer.id}"
    raise TypeError("Unsupported chatbot session owner")
