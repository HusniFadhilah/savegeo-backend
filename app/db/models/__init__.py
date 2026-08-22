"""Import every model so `Base.metadata` is complete for Alembic autogenerate,
`Base.metadata.create_all()` in tests, and relationship string-name resolution.
"""
from app.db.models.admin_user import AdminUser
from app.db.models.analysis_result import AnalysisResult
from app.db.models.analysis_run import AnalysisRun
from app.db.models.audit_log import AuditLog
from app.db.models.chat_message import ChatMessage
from app.db.models.chat_session import ChatSession
from app.db.models.company_boundary import CompanyBoundary
from app.db.models.dataset_entry import DatasetEntry
from app.db.models.disaster_aoi import DisasterAOI
from app.db.models.disaster_event import DisasterEvent
from app.db.models.field import Field
from app.db.models.gee_credential import GEECredential
from app.db.models.hotspot import Hotspot
from app.db.models.role import Permission, Role, role_permissions
from app.db.models.satellite_imagery import SatelliteImagery
from app.db.models.satellite_provider_entry import SatelliteProviderEntry
from app.db.models.system_config import SystemConfig
from app.db.models.uploaded_model import UploadedModel
from app.db.models.user import User

__all__ = [
    "AdminUser",
    "AnalysisResult",
    "AnalysisRun",
    "AuditLog",
    "ChatMessage",
    "ChatSession",
    "CompanyBoundary",
    "DatasetEntry",
    "DisasterAOI",
    "DisasterEvent",
    "Field",
    "GEECredential",
    "Hotspot",
    "Permission",
    "Role",
    "SatelliteImagery",
    "SatelliteProviderEntry",
    "SystemConfig",
    "UploadedModel",
    "User",
    "role_permissions",
]
