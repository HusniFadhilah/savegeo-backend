"""Version-aware, source-linked methodology review aids (not certification rules)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.dmrv_integrity import DmrvIntegrityRecord
from app.db.models.dmrv_project import DmrvProject
from app.services.dmrv.authz import Principal, authorize
from app.services.dmrv.errors import DmrvError

VERRA_VM0047 = "https://verra.org/methodologies/vm0047-afforestation-reforestation-and-revegetation-v1-1/"
VERRA_DSP = "https://verra.org/methodologies-main/application-for-listed-data-service-providers-under-vm0047/"
VERRA_REVIEWER = "https://verra.org/verra-releases-new-resources-for-arr-projects/"
VERRA_VMD0054 = "https://verra.org/methodologies/vmd0054-estimating-leakage-from-the-displacement-of-agricultural-activities-v1-1/"

RULES = [
    {"key": "approach", "title": "Approach and applicability", "description": "Record the selected area-based or census-based approach, activity type, and evidence that the project meets that approach's applicability conditions.", "source": VERRA_VM0047, "required": True},
    {"key": "project_boundary_and_start", "title": "Project boundary and start date", "description": "Retain the georeferenced project boundary, version history, start date, eligibility evidence, and boundary updates used for accounting.", "source": VERRA_VM0047, "required": True},
    {"key": "land_eligibility_and_baseline", "title": "Land eligibility and baseline", "description": "Document prior land use, land eligibility, baseline scenario, historical evidence, and the basis for the selected reference period.", "source": VERRA_VM0047, "required": True},
    {"key": "additionality", "title": "Additionality and regulatory surplus", "description": "Retain the applicable additionality assessment and evidence for regulatory surplus, investment/barrier, or performance benchmark routes as required by the project and method.", "source": VERRA_VM0047, "required": True},
    {"key": "project_activities_and_monitoring", "title": "Project activities and monitoring plan", "description": "Record the activity schedule, implementation evidence, monitoring parameters, frequency, responsibilities, and changes from the validated plan.", "source": VERRA_VM0047, "required": True},
    {"key": "pool_selection_and_accounting", "title": "Carbon pools and accounting", "description": "List included/excluded pools, measurement methods, stock-change data, uncertainty, project emissions, and reasons for permitted exclusions.", "source": VERRA_VM0047, "required": True},
    {"key": "dynamic_performance_benchmark", "title": "Dynamic performance benchmark", "description": "For area-based projects, retain applicable control-plot design, matched project/control Stocking Index series, annual performance benchmark, and calculations for each monitoring/verification period.", "source": VERRA_VM0047, "required": True, "approach": "area_based"},
    {"key": "stocking_index_specification", "title": "Stocking Index specification and resolution", "description": "Record product/provider, pixel size, calibration/validation relationship to AGB, and current Verra listed-provider requirements; resolution alone does not establish compliance.", "source": VERRA_DSP, "required": True, "approach": "area_based"},
    {"key": "stocking_index_temporal_quality", "title": "Annual coverage, seasonality, and image quality", "description": "Record annual baseline/monitoring coverage, seasonal window, observation gaps, quality filters, sensor/product versions, and time-series comparability evidence.", "source": VERRA_DSP, "required": True, "approach": "area_based"},
    {"key": "census_eligibility_and_records", "title": "Census eligibility and planting records", "description": "For census-based projects, retain evidence for census eligibility, unchanged land use, direct planting, census/plot protocol, planting-unit counts, survival, and initial stock where applicable.", "source": VERRA_VM0047, "required": True, "approach": "census_based"},
    {"key": "emissions_and_uncertainty", "title": "Emission sources and uncertainty", "description": "Document applicable project GHG sources, measurement/model uncertainty, sampling and spatial design, correlation assumptions, and reproducible supporting calculations.", "source": VERRA_VM0047, "required": True},
    {"key": "leakage", "title": "Leakage and VMD0054 applicability", "description": "For area-based projects, document leakage applicability and the VMD0054 pathway, historical production/reference window, mitigation, cross-commodity treatment, factors, evidence, and monitoring duration. Census-based projects must document the method's stated treatment.", "source": VERRA_VMD0054, "required": True},
    {"key": "non_permanence_and_safeguards", "title": "Non-permanence and safeguards", "description": "Retain reversal-risk assessment/buffer evidence, safeguards, stakeholder engagement, grievance handling, and monitoring of identified risks where applicable.", "source": VERRA_VM0047, "required": True},
    {"key": "verification_package", "title": "Verification package and independent review", "description": "Keep traceable versions of monitoring reports, calculations, source data, reviewer findings, corrective actions, and submitted evidence for the verification period.", "source": VERRA_REVIEWER, "required": True},
]

def normalize(value: str | None) -> str:
    return "".join((value or "").lower().replace(" ", "").replace("_", "").split())


def _supported(project: DmrvProject) -> bool:
    return normalize(project.methodology_id) in {"vm0047", "verravm0047"} and normalize(
        project.methodology_version
    ) in {"1.1", "v1.1"}


def get_checklist(db: Session, principal: Principal, project_id: str) -> dict:
    authorize(db, project_id, principal, "accounting.read")
    project = db.get(DmrvProject, project_id)
    if project is None:
        raise DmrvError(404, "not_found", "Proyek tidak ditemukan.")
    supported = _supported(project)
    assessment = db.execute(
        select(DmrvIntegrityRecord).where(
            DmrvIntegrityRecord.project_id == project_id,
            DmrvIntegrityRecord.category == "methodology_check",
            DmrvIntegrityRecord.record_key == "current",
        )
    ).scalar_one_or_none()
    approach = assessment.payload.get("approach") if assessment else "area_based"
    saved = {item["key"]: item for item in (assessment.payload.get("items", []) if assessment else [])}
    rules = []
    for rule in RULES if supported else []:
        saved_item = saved.get(rule["key"], {})
        initial_status = "not_applicable" if rule.get("approach") and rule["approach"] != approach else "not_assessed"
        rules.append({**rule, "status": saved_item.get("status", initial_status),
                      "notes": saved_item.get("notes", ""), "evidence": saved_item.get("evidence", [])})
    return {
        "methodology_id": project.methodology_id,
        "methodology_version": project.methodology_version,
        "supported": supported,
        "pack_id": "verra-vm0047-v1.1-savegeo-review-aid" if supported else None,
        "reviewed_on": "2026-11-24" if supported else None,
        "approach": approach if supported else None,
        "approach_options": ["area_based", "census_based"] if supported else [],
        "items": rules,
        "record_id": assessment.id if assessment else None,
        "revision": assessment.revision if assessment else 0,
        "record_status": assessment.status if assessment else None,
        "qualification": (
            "Curated internal checklist based on linked public Verra resources. It is not exhaustive, does not replace the current methodology text or project-specific VVB review, and does not determine eligibility or certification."
            if supported else
            "No curated checklist is available for this exact methodology/version. Use the project's official methodology and reviewer guidance; do not apply another version's criteria."
        ),
    }


def validate_checklist(db: Session, project_id: str, approach: str, items: list[dict]) -> None:
    project = db.get(DmrvProject, project_id)
    if project is None or not _supported(project):
        raise DmrvError(422, "methodology_pack_unavailable", "Paket aturan hanya tersedia untuk VM0047 v1.1.")
    keys = {rule["key"] for rule in RULES}
    if {item["key"] for item in items} != keys:
        raise DmrvError(422, "methodology_items_mismatch", "Daftar butir harus mengikuti paket metodologi proyek.")
    for item in items:
        rule = next(rule for rule in RULES if rule["key"] == item["key"])
        conditional_approach = rule.get("approach")
        should_be_not_applicable = bool(conditional_approach and conditional_approach != approach)
        if (item["status"] == "not_applicable") != should_be_not_applicable:
            raise DmrvError(422, "methodology_applicability_mismatch", "Tandai butir bersyarat sesuai pendekatan proyek.")
