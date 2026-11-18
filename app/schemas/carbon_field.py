"""Explicit field units and sampling support; no species or site defaults."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SoilLayer(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    top_cm: float = Field(ge=0, le=1000)
    bottom_cm: float = Field(gt=0, le=1000)
    organic_carbon_g_kg: float = Field(ge=0, le=1000)
    bulk_density_g_cm3: float = Field(gt=0, le=3)
    coarse_fragments_volume_pct: float = Field(ge=0, lt=100)
    bulk_density_basis: Literal["fine_earth_per_fine_earth_volume"]


class FieldPrediction(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)
    source: str = Field(min_length=3, max_length=255)
    year: int = Field(ge=1900, le=2100)
    value_mg_c_ha: float = Field(ge=0)
    scale_m: float = Field(gt=0)
    support: Literal["plot_mean", "point_pixel"]
    depth_top_cm: float | None = Field(default=None, ge=0)
    depth_bottom_cm: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_depth(self):
        if (self.depth_top_cm is None) != (self.depth_bottom_cm is None):
            raise ValueError("Kedalaman prediksi harus memuat batas atas dan bawah.")
        if self.depth_top_cm is not None and self.depth_bottom_cm <= self.depth_top_cm:
            raise ValueError("Batas bawah prediksi harus lebih dalam dari batas atas.")
        return self


class LitterSample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    wet_mass_kg: float = Field(gt=0)
    subsample_wet_kg: float = Field(gt=0)
    subsample_dry_kg: float = Field(ge=0)
    quadrat_count: int = Field(ge=1, le=1000)
    quadrat_area_m2: float = Field(gt=0)
    maximum_diameter_cm: float = Field(gt=0)

    @model_validator(mode="after")
    def consistent_masses(self):
        if self.subsample_dry_kg > self.subsample_wet_kg or self.subsample_wet_kg > self.wet_mass_kg:
            raise ValueError("Berat kering ≤ berat basah subsampel ≤ berat basah komposit.")
        return self


class DeadwoodPiece(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)
    component: Literal["standing", "downed"]
    volume_m3: float = Field(gt=0)
    dry_density_kg_m3: float = Field(gt=0, le=2000)
    decay_class: str = Field(min_length=1, max_length=64)
    density_source: str = Field(min_length=3, max_length=1000)


class DeadwoodSample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    minimum_diameter_cm: float = Field(gt=0)
    pieces: list[DeadwoodPiece] = Field(min_length=1, max_length=500)


class FieldObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)
    plot_id: str = Field(min_length=1, max_length=64)
    survey_date: date
    pool: Literal["agb", "bgb", "soc", "litter", "deadwood"]
    role: Literal["calibration", "validation"]
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    plot_area_m2: float = Field(gt=0)
    reference_type: Literal["field_measurement", "allometry", "root_shoot_ratio", "laboratory"]
    method: str = Field(min_length=3, max_length=1000)
    source: str = Field(min_length=3, max_length=1000)
    quantity: Literal["carbon", "dry_biomass"] = "carbon"
    unit: Literal["Mg/ha", "kg/plot"] = "Mg/ha"
    value: float | None = Field(default=None, ge=0)
    carbon_fraction: float | None = Field(default=None, gt=0, lt=1)
    depth_top_cm: float | None = Field(default=None, ge=0)
    depth_bottom_cm: float | None = Field(default=None, gt=0, le=1000)
    soil_layers: list[SoilLayer] = Field(default_factory=list, max_length=30)
    prediction: FieldPrediction | None = None
    litter_sample: LitterSample | None = None
    deadwood_sample: DeadwoodSample | None = None

    @model_validator(mode="after")
    def sampling_support(self):
        if self.litter_sample or self.deadwood_sample:
            if self.value is not None or self.soil_layers or (self.litter_sample and self.deadwood_sample):
                raise ValueError(
                    "Pilih satu cara input: nilai akhir, sampel serasah, volume kayu mati, atau lapisan SOC."
                )
            if (self.litter_sample and self.pool != "litter") or (
                self.deadwood_sample and self.pool != "deadwood"
            ):
                raise ValueError("Jenis sampel harus sesuai dengan pool.")
            if self.quantity != "dry_biomass" or self.unit != "kg/plot":
                raise ValueError(
                    "Sampel serasah/volume kayu mati menggunakan biomassa kering dalam kg/plot dengan fraksi karbon eksplisit."
                )
            if (
                self.litter_sample
                and self.litter_sample.quadrat_count * self.litter_sample.quadrat_area_m2 > self.plot_area_m2
            ):
                raise ValueError("Total luas kuadrat serasah tidak boleh melebihi luas plot induk.")
        if self.survey_date > date.today():
            raise ValueError("Tanggal survei tidak boleh di masa depan.")
        if self.pool == "soc":
            if self.quantity != "carbon" or self.reference_type != "laboratory":
                raise ValueError("SOC memerlukan hasil karbon laboratorium dan metode analisisnya.")
            if (
                self.depth_top_cm is None
                or self.depth_bottom_cm is None
                or self.depth_bottom_cm <= self.depth_top_cm
            ):
                raise ValueError("Rentang kedalaman SOC wajib dan harus berurutan.")
            if self.soil_layers:
                if self.unit != "Mg/ha":
                    raise ValueError("Lapisan SOC dihitung sebagai stok Mg C/ha, bukan massa kg/plot.")
                if self.value is not None:
                    raise ValueError("Isi stok SOC langsung atau lapisan tanah, bukan keduanya.")
                cursor = self.depth_top_cm
                for layer in sorted(self.soil_layers, key=lambda item: item.top_cm):
                    if abs(layer.top_cm - cursor) > 1e-6 or layer.bottom_cm <= layer.top_cm:
                        raise ValueError("Lapisan SOC harus berurutan, tanpa celah atau tumpang tindih.")
                    cursor = layer.bottom_cm
                if abs(cursor - self.depth_bottom_cm) > 1e-6:
                    raise ValueError("Lapisan SOC harus mencakup seluruh kedalaman target.")
        elif self.soil_layers or self.depth_top_cm is not None or self.depth_bottom_cm is not None:
            raise ValueError("Kedalaman/lapisan tanah hanya berlaku untuk SOC.")
        if (
            not self.soil_layers
            and not self.litter_sample
            and not self.deadwood_sample
            and self.value is None
        ):
            raise ValueError("Nilai hasil lapangan wajib diisi.")
        if self.quantity == "dry_biomass" and self.carbon_fraction is None:
            raise ValueError("Fraksi karbon wajib untuk konversi biomassa kering menjadi karbon.")
        if self.pool != "bgb" and self.reference_type == "root_shoot_ratio":
            raise ValueError("Root:shoot ratio hanya berlaku untuk BGB.")
        return self


class FieldObservationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[FieldObservationInput] = Field(min_length=1, max_length=2000)


class FieldAssessmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    pool: Literal["agb", "bgb", "soc", "litter", "deadwood"]
    prediction_source: str = Field(min_length=3, max_length=255)
    max_year_gap: int = Field(default=0, ge=0, le=5)
    allow_point_pixel: bool = False
    create_calibration_candidate: bool = True
    depth_top_cm: float | None = Field(default=None, ge=0)
    depth_bottom_cm: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def depth_support(self):
        if self.pool == "soc" and (
            self.depth_top_cm is None
            or self.depth_bottom_cm is None
            or self.depth_bottom_cm <= self.depth_top_cm
        ):
            raise ValueError("Pilih kedalaman SOC yang sama untuk semua pasangan validasi.")
        return self


class FieldPredictionExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    pool: Literal["agb", "bgb", "soc", "litter", "deadwood"]
    analysis_year: int = Field(ge=1984, le=2100)
    model_name: str | None = Field(default=None, min_length=1, max_length=255)
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=250, ge=1, le=500)
