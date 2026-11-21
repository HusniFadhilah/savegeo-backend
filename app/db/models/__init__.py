"""Import every model so `Base.metadata` is complete for Alembic autogenerate,
`Base.metadata.create_all()` in tests, and relationship string-name resolution.
"""

from app.db.models.admin_user import AdminUser
from app.db.models.admin_mfa import AdminMfa, MfaChallenge
from app.db.models.workspace_analysis import WorkspaceAnalysisRun, WorkspaceQuota, WorkspaceUsageEvent
from app.db.models.analysis_result import AnalysisResult
from app.db.models.analysis_run import AnalysisRun
from app.db.models.analysis_provenance import AnalysisProvenance
from app.db.models.analysis_output import AnalysisOutput
from app.db.models.carbon_inference_cache import CarbonInferenceCache
from app.db.models.carbon_annual_estimate import CarbonAnnualEstimate
from app.db.models.crop_monitoring_cache import CropMonitoringCache
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
from app.db.models.wildfire_hotspot import WildfireHotspot
from app.db.models.role import Permission, Role, role_permissions
from app.db.models.satellite_imagery import SatelliteImagery
from app.db.models.satellite_provider_entry import SatelliteProviderEntry
from app.db.models.imagery_provider_entry import ImageryProviderEntry
from app.db.models.system_config import SystemConfig
from app.db.models.uploaded_model import UploadedModel
from app.db.models.user import User
from app.db.models.workflow import Workflow, WorkflowRun
from app.db.models.dmrv_project import DmrvProject, ProjectMember
from app.db.models.dmrv_integrity import DmrvIntegrityRecord, DmrvIntegrityRevision, DmrvIntegrityDocument, DmrvRiskPolicy
from app.db.models.dmrv_advanced import DmrvCreditEvent, DmrvFieldCapture
from app.db.models.organization import Organization, OrganizationMember, OrganizationInvitation, WorkspaceRequest
from app.db.models.dmrv_boundary import BoundaryVersion
from app.db.models.dmrv_audit import DmrvAuditEvent, DmrvIdempotencyKey
from app.db.models.dmrv_field import ImportBatch, SurveyPlot, SurveyVisit, TreeMeasurement
from app.db.models.dmrv_job import DmrvJob, DmrvSnapshot
from app.db.models.dmrv_monitoring import DmrvMonitoringPeriod
from app.db.models.dmrv_evidence import DmrvEvidencePackage
from app.db.models.dmrv_submission import DmrvSubmission, DmrvReviewFinding
from app.db.models.carbon_calibration import (
    CarbonFieldObservation,
    CarbonCalibrationDataset,
    CarbonCalibrationFile,
    CarbonCalibrationModel,
    CarbonCalibrationRun,
    FieldPlot,
    FieldTree,
)
from app.db.models.green_ranking import GreenRankingSnapshot, GreenRankingSnapshotRow

__all__ = [
    "DmrvIntegrityRecord", "DmrvIntegrityRevision", "DmrvIntegrityDocument", "DmrvRiskPolicy",
    "DmrvCreditEvent", "DmrvFieldCapture",
    "Organization", "OrganizationMember", "OrganizationInvitation", "WorkspaceRequest",
    "AdminMfa", "MfaChallenge", "WorkspaceAnalysisRun", "WorkspaceQuota", "WorkspaceUsageEvent",
    "CarbonFieldObservation",
    "GreenRankingSnapshot",
    "GreenRankingSnapshotRow",
    "AdminUser",
    "AnalysisResult",
    "AnalysisRun",
    "AnalysisProvenance",
    "AnalysisOutput",
    "CarbonInferenceCache",
    "CarbonAnnualEstimate",
    "CropMonitoringCache",
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
    "WildfireHotspot",
    "Permission",
    "Role",
    "SatelliteImagery",
    "SatelliteProviderEntry",
    "ImageryProviderEntry",
    "SystemConfig",
    "UploadedModel",
    "User",
    "Workflow",
    "WorkflowRun",
    "role_permissions",
    "CarbonCalibrationDataset",
    "CarbonCalibrationFile",
    "CarbonCalibrationModel",
    "CarbonCalibrationRun",
    "FieldPlot",
    "FieldTree",
    "DmrvProject",
    "ProjectMember",
    "BoundaryVersion",
    "DmrvAuditEvent",
    "DmrvIdempotencyKey",
    "ImportBatch",
    "SurveyPlot",
    "SurveyVisit",
    "TreeMeasurement",
    "DmrvJob",
    "DmrvSnapshot",
    "DmrvMonitoringPeriod",
    "DmrvEvidencePackage",
    "DmrvSubmission",
    "DmrvReviewFinding",
]
