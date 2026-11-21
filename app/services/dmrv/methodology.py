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

RULES = [
    {"key": "approach", "title": "Approach and applicability", "description": "Record whether the project uses the area-based or census-based approach and document why it fits the activity.", "source": VERRA_VM0047, "required": True},
    {"key": "activity_eligibility", "title": "ARR activity and land eligibility", "description": "Document the activity, land-use context, project start conditions, and eligibility checks for this project.", "source": VERRA_VM0047, "required": True},
    {"key": "dynamic_performance_benchmark", "title": "Dynamic performance benchmark", "description": "For area-based projects, retain project/control plot inputs and the performance-benchmark method and outputs for each verification.", "source": VERRA_VM0047, "required": True, "approach": "area_based"},
    {"key": "census_planting_records", "title": "Census and planting records", "description": "For census-based projects, retain the census protocol, planting-unit records, and project-method additionality evidence.", "source": VERRA_VM0047, "required": True, "approach": "census_based"},
    {"key": "stocking_index_resolution", "title": "Stocking Index resolution", "description": "For the area-based approach's Stocking Index product, record final pixel size and specification. Verra's listed-provider requirements specify ≤30 m.", "source": VERRA_DSP, "required": True, "approach": "area_based"},
    {"key": "stocking_index_annual", "title": "Annual Stocking Index coverage", "description": "For area-based projects, record annual availability for relevant baseline and monitoring years, including gaps.", "source": VERRA_DSP, "required": True, "approach": "area_based"},
    {"key": "seasonality", "title": "Seasonal consistency", "description": "For optical imagery in the Stocking Index, document the consistent seasonal window and quality filters.", "source": VERRA_DSP, "required": True, "approach": "area_based"},
    {"key": "agb_correlation", "title": "Stocking Index and AGB relationship", "description": "For area-based Stocking Index, retain evidence of correlation with AGB in the forest type or ecoregion and explain project-level assessment.", "source": VERRA_DSP, "required": True, "approach": "area_based"},
    {"key": "leakage", "title": "Leakage applicability", "description": "For area-based projects, document leakage assessment under VMD0054 where applicable; for census-based projects, document the basis for the methodology's stated leakage treatment.", "source": VERRA_VM0047, "required": True},
    {"key": "monitoring_and_calculation", "title": "Monitoring and quantification", "description": "Retain monitoring-period inputs, carbon-stock calculations, uncertainty treatment, and reproducible supporting data for reviewer assessment.", "source": VERRA_REVIEWER, "required": True},
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
        "reviewed_on": "2026-11-21" if supported else None,
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

\n