from __future__ import annotations

from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator


TARGET_POOLS = {
    "aboveground_biomass_carbon",
    "belowground_biomass_carbon",
    "living_biomass_carbon",
    "soil_organic_carbon",
    "litter_carbon",
    "deadwood_carbon",
}


class CalibrationSourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=3, max_length=200)
    url: AnyHttpUrl

    @field_validator("url")
    @classmethod
    def validate_source_url(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        # These references are displayed as links only. Restrict them to the
        # Google Drive hosts used by the reviewed source documents; the server
        # never fetches a URL supplied in this metadata.
        if (
            value.scheme != "https"
            or value.username is not None
            or value.password is not None
            or (value.host or "").lower() not in {"drive.google.com", "docs.google.com"}
        ):
            raise ValueError("Source links must use HTTPS on an approved Google Drive host")
        return value


class ReportedCarbonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vegetation_area_ha_pdf: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    agb_ton_c_workbook: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    agb_ton_c_pdf_table: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    soc_ton_c_workbook: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    soc_ton_c_pdf_table: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    soc_difference_ton_c: float | None = Field(default=None, allow_inf_nan=False)
    summary_use: Literal["context_only_not_training_labels"]


class CalibrationReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plot_observations_imported: bool
    plot_area_confirmed: bool
    coordinate_review_required: bool
    soc_totals_reconciled: bool
    unsupported_pools: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("unsupported_pools")
    @classmethod
    def validate_unsupported_pools(cls, value: list[str]) -> list[str]:
        unknown = set(value) - TARGET_POOLS
        if unknown:
            raise ValueError(f"Unsupported target pool(s): {sorted(unknown)}")
        return value


class CalibrationDatasetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,127}$")
    name: str = Field(min_length=3, max_length=255)
    site: str = "Ring 3 PT Dahana"
    location: str = "Subang, Jawa Barat"
    crs: str = "EPSG:32748"
    survey_year: int = 2026
    ecosystem: str = "rubber_plantation"
    species: list[str] = ["Hevea brasiliensis"]
    sampling_design: str = "purposive"
    # Zero is valid for a source-only draft whose field plots have not yet been
    # extracted and reviewed. Such a draft cannot be validated for calibration.
    plot_count: int = Field(default=10, ge=0)
    plot_size_m: list[float] = [20, 20]
    sample_area_ha: float = 0.4
    mapped_area_ha: float = 9
    target_pools: list[str] = [
        "aboveground_biomass_carbon",
        "belowground_biomass_carbon",
        "living_biomass_carbon",
    ]
    carbon_fraction: float = Field(default=0.47, gt=0, lt=1)
    field_reference_type: str = "allometric"
    access: str = "restricted"
    status: str = "draft"
    soil_depth_cm: list[float] | None = None
    application_domain_geojson: dict[str, Any] | None = None
    compatible_sensors: list[str] = Field(default_factory=list, max_length=8)
    preprocessing_version: str | None = Field(default=None, min_length=1, max_length=128)
    source_reference_only: bool = False
    source_references: list[CalibrationSourceReference] = Field(default_factory=list, max_length=20)
    reported_2026_summary: ReportedCarbonSummary | None = None
    calibration_readiness: CalibrationReadiness | None = None

    @model_validator(mode="after")
    def validate_source_only_dataset(self) -> CalibrationDatasetCreate:
        if self.source_reference_only and self.plot_count != 0:
            raise ValueError("A source-reference draft cannot claim unimported field plots")
        if self.source_reference_only and not self.source_references:
            raise ValueError("A source-reference draft must include at least one reviewed source link")
        if self.application_domain_geojson is not None:
            from shapely.geometry import shape

            value = self.application_domain_geojson
            if value.get("type") == "Feature":
                value = value.get("geometry") or {}
            geometry = shape(value)
            if geometry.geom_type not in {"Polygon", "MultiPolygon"} or geometry.is_empty or not geometry.is_valid:
                raise ValueError("Calibration application domain must be a valid Polygon or MultiPolygon.")
        if any(not item.strip() or len(item) > 64 for item in self.compatible_sensors):
            raise ValueError("Compatible sensor IDs must be non-empty strings up to 64 characters.")
        return self

    @field_validator("target_pools")
    @classmethod
    def validate_target_pools(cls, value: list[str]) -> list[str]:
        unknown = set(value) - TARGET_POOLS
        if unknown:
            raise ValueError(f"Unsupported target pool(s): {sorted(unknown)}")
        return value

    @field_validator("compatible_sensors")
    @classmethod
    def validate_compatible_sensors(cls, value: list[str]) -> list[str]:
        normalized = [item.strip().lower() for item in value]
        supported = {"sentinel-1", "sentinel-2", "landsat"}
        if any(item not in supported for item in normalized):
            raise ValueError(f"Compatible sensors must be selected from {sorted(supported)}.")
        return sorted(set(normalized))

    def manifest(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class CalibrationRunCreate(BaseModel):
    dataset_id: str
    method: str = Field(
        default="linear_calibration",
        pattern=r"^(baseline|intercept_bias|linear_calibration|residual_calibration|uav_local_experimental)$",
    )
    target_pool: str = "aboveground_biomass_carbon"
    parameters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("target_pool")
    @classmethod
    def validate_target(cls, value: str) -> str:
        if value not in TARGET_POOLS:
            raise ValueError(f"Unsupported target pool: {value}")
        return value


class CalibrationReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=1000)
