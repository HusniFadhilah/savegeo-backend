from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


TARGET_POOLS = {
    "aboveground_biomass_carbon",
    "belowground_biomass_carbon",
    "living_biomass_carbon",
}


class CalibrationDatasetCreate(BaseModel):
    dataset_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,127}$")
    name: str = Field(min_length=3, max_length=255)
    site: str = "Ring 3 PT Dahana"
    location: str = "Subang, Jawa Barat"
    crs: str = "EPSG:32748"
    survey_year: int = 2026
    ecosystem: str = "rubber_plantation"
    species: list[str] = ["Hevea brasiliensis"]
    sampling_design: str = "purposive"
    plot_count: int = Field(default=10, ge=1)
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

    @field_validator("target_pools")
    @classmethod
    def validate_target_pools(cls, value: list[str]) -> list[str]:
        unknown = set(value) - TARGET_POOLS
        if unknown:
            raise ValueError(f"Unsupported target pool(s): {sorted(unknown)}")
        return value

    def manifest(self) -> dict[str, Any]:
        return self.model_dump()


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
