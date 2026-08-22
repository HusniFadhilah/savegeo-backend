"""Import every model so `Base.metadata` is complete for Alembic autogenerate,
`Base.metadata.create_all()` in tests, and relationship string-name resolution.
"""
from app.db.models.role import Role, Permission, role_permissions  # noqa: F401
from app.db.models.admin_user import AdminUser  # noqa: F401
from app.db.models.gee_credential import GEECredential  # noqa: F401
from app.db.models.uploaded_model import UploadedModel  # noqa: F401
from app.db.models.system_config import SystemConfig  # noqa: F401
from app.db.models.chat_session import ChatSession  # noqa: F401
from app.db.models.chat_message import ChatMessage  # noqa: F401
from app.db.models.company_boundary import CompanyBoundary  # noqa: F401
from app.db.models.dataset_entry import DatasetEntry  # noqa: F401
from app.db.models.audit_log import AuditLog  # noqa: F401
from app.db.models.satellite_provider_entry import SatelliteProviderEntry  # noqa: F401
from app.db.models.user import User  # noqa: F401
from app.db.models.disaster_event import DisasterEvent  # noqa: F401
from app.db.models.disaster_aoi import DisasterAOI  # noqa: F401
from app.db.models.satellite_imagery import SatelliteImagery  # noqa: F401
from app.db.models.analysis_run import AnalysisRun  # noqa: F401
from app.db.models.analysis_result import AnalysisResult  # noqa: F401
from app.db.models.hotspot import Hotspot  # noqa: F401
from app.db.models.field import Field  # noqa: F401

__all__ = [
    "Role",
    "Permission",
    "role_permissions",
    "AdminUser",
    "GEECredential",
    "UploadedModel",
    "SystemConfig",
    "ChatSession",
    "ChatMessage",
    "CompanyBoundary",
    "DatasetEntry",
    "AuditLog",
    "SatelliteProviderEntry",
    "User",
    "DisasterEvent",
    "DisasterAOI",
    "SatelliteImagery",
    "AnalysisRun",
    "AnalysisResult",
    "Hotspot",
    "Field",
]
