"""Bounded schemas for the carbon MRV extension workflows."""
from __future__ import annotations

import datetime as dt
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


class BenchmarkPlot(Strict):
    plot_id: str = Field(min_length=1, max_length=80)
    years: list[int] = Field(default_factory=list, min_length=0, max_length=30)
    weight: float = Field(default=1.0, gt=0, le=1e9)
    project_si: list[float] = Field(min_length=3, max_length=30)
    control_si: list[float] = Field(min_length=3, max_length=30)

    @model_validator(mode="after")
    def matched_series(self):
        if len(self.project_si) != len(self.control_si):
            raise ValueError("Seri Stocking Index proyek dan kontrol harus berpasangan.")
        if self.years and (len(self.years) != len(self.project_si) or self.years != sorted(set(self.years))):
            raise ValueError("Tahun Stocking Index harus unik, berurutan, dan cocok dengan panjang seri.")
        if any(value < 0 or value > 1e6 for value in self.project_si + self.control_si):
            raise ValueError("Nilai Stocking Index di luar rentang yang didukung.")
        return self


class MethodPoolMeasurement(Strict):
    pool: Literal["AGB", "BGB", "SOC", "litter", "deadwood"]
    start_mg_c_ha: float = Field(ge=0, le=1e7)
    end_mg_c_ha: float = Field(ge=0, le=1e7)
    start_se_mg_c_ha: float = Field(default=0, ge=0, le=1e7)
    end_se_mg_c_ha: float = Field(default=0, ge=0, le=1e7)
    temporal_correlation: float = Field(default=0, ge=-1, le=1)
    cumulative_start_change_mg_c_ha: float | None = Field(default=None, ge=-1e7, le=1e7)
    cumulative_end_change_mg_c_ha: float | None = Field(default=None, ge=-1e7, le=1e7)


class MethodologyCalculationInput(Strict):
    methodology_id: Literal["VM0047"] = "VM0047"
    methodology_version: Literal["1.1"] = "1.1"
    approach: Literal["area_based", "census_based"]
    monitoring_period_id: str | None = Field(default=None, min_length=1, max_length=36)
    period_start: dt.date
    period_end: dt.date
    area_ha: float = Field(gt=0, le=1e7)
    project_emissions_tco2e: float = Field(default=0, ge=0, le=1e12)
    leakage_tco2e: float = Field(default=0, ge=0, le=1e12)
    leakage_record_id: str | None = Field(default=None, min_length=1, max_length=36)
    uncertainty_record_id: str | None = Field(default=None, min_length=1, max_length=36)
    leakage_assessment_basis: str = Field(default="", max_length=4000)
    pools: list[MethodPoolMeasurement] = Field(default_factory=list, max_length=5)
    plots: list[BenchmarkPlot] = Field(default_factory=list, max_length=2000)
    census_planting_units: int | None = Field(default=None, ge=1, le=1000000000)
    census_sampled_units: int | None = Field(default=None, ge=1, le=1000000)
    census_surviving_sampled_units: int | None = Field(default=None, ge=0, le=1000000)
    census_mean_live_unit_mg_c: float | None = Field(default=None, ge=0, le=1e5)
    census_mean_live_unit_se_mg_c: float | None = Field(default=None, ge=0, le=1e5)
    census_start_live_units: int | None = Field(default=None, ge=0, le=1000000000)
    census_start_mean_live_unit_mg_c: float | None = Field(default=None, ge=0, le=1e5)
    census_start_mean_live_unit_se_mg_c: float | None = Field(default=None, ge=0, le=1e5)
    planting_density_per_ha: float | None = Field(default=None, ge=0, le=50)
    census_land_use_unchanged: bool = False
    census_direct_planting: bool = False
    census_preexisting_woody_cover_pct: float | None = Field(default=None, ge=0, le=100)
    census_site_prep_soil_disturbance: bool | None = None
    assessment_evidence_complete: bool = False
    evidence: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid_period_and_inputs(self):
        if self.period_end <= self.period_start:
            raise ValueError("Periode monitoring harus berurutan dan lebih dari satu hari.")
        if len({pool.pool for pool in self.pools}) != len(self.pools):
            raise ValueError("Pool tidak boleh berulang.")
        for pool in self.pools:
            if (pool.cumulative_start_change_mg_c_ha is None) != (pool.cumulative_end_change_mg_c_ha is None):
                raise ValueError("Perubahan kumulatif awal dan akhir harus diisi bersamaan untuk satu pool.")
        if self.approach == "area_based" and (self.census_planting_units is not None or not self.plots or not self.pools):
            raise ValueError("Area-based memerlukan seri plot berpasangan, bukan input sensus.")
        if self.approach == "area_based":
            if len({plot.plot_id for plot in self.plots}) != len(self.plots):
                raise ValueError("ID plot area-based harus unik.")
            if len({len(plot.project_si) for plot in self.plots}) != 1:
                raise ValueError("Semua plot harus memiliki jumlah tahun Stocking Index yang sama.")
            aligned_years = [plot.years or list(range(len(plot.project_si))) for plot in self.plots]
            if any(years != aligned_years[0] for years in aligned_years[1:]):
                raise ValueError("Semua pasangan plot harus memakai tahun observasi yang sama.")
        if self.approach == "census_based":
            if self.pools:
                raise ValueError("Census-based memakai estimasi unit biomassa berkayu; masukkan pool melalui seri unit census, bukan inventaris pool area-based.")
            required = (self.census_planting_units, self.census_sampled_units,
                        self.census_surviving_sampled_units, self.census_mean_live_unit_mg_c,
                        self.planting_density_per_ha)
            if any(value is None for value in required):
                raise ValueError("Input sensus dan kepadatan penanaman wajib diisi.")
            if self.census_surviving_sampled_units > self.census_sampled_units:
                raise ValueError("Jumlah unit hidup sampel melebihi jumlah unit yang diukur.")
            if self.census_planting_units < self.census_sampled_units:
                raise ValueError("Jumlah unit sampel melebihi jumlah unit tanam.")
            if not self.census_land_use_unchanged:
                raise ValueError("Pendekatan census memerlukan konfirmasi penggunaan lahan tidak berubah.")
            if self.planting_density_per_ha <= 0:
                raise ValueError("Kepadatan penanaman census harus lebih dari nol.")
            if (self.census_start_live_units is None) != (self.census_start_mean_live_unit_mg_c is None):
                raise ValueError("Jumlah unit hidup dan biomassa rata-rata census awal harus diisi bersamaan.")
            if self.census_start_mean_live_unit_se_mg_c is not None and self.census_start_mean_live_unit_mg_c is None:
                raise ValueError("SE census awal memerlukan biomassa rata-rata awal.")
            if self.leakage_tco2e != 0:
                raise ValueError("VM0047 census-based tidak memakai pengurangan leakage; tinjau emisi lain terpisah.")
        return self


class UncertaintyComponent(Strict):
    source: Literal["model", "field_validation", "sampling_design", "spatial_variation", "emission_factor", "leakage"]
    standard_uncertainty_tco2e: float | None = Field(default=None, ge=0, le=1e12)
    observations_tco2e: list[float] = Field(default_factory=list, max_length=10000)
    observation_ids: list[str] = Field(default_factory=list, max_length=10000)
    evidence: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def has_measurement_basis(self):
        if self.standard_uncertainty_tco2e is None and len(self.observations_tco2e) < 2:
            raise ValueError("Isi SE tervalidasi atau minimal dua observasi untuk menurunkan SE.")
        if self.standard_uncertainty_tco2e is not None and self.observations_tco2e:
            raise ValueError("Pilih SE tervalidasi atau seri residual; jangan masukkan keduanya untuk sumber yang sama.")
        if self.observation_ids and (
            len(self.observation_ids) != len(self.observations_tco2e)
            or len(set(self.observation_ids)) != len(self.observation_ids)
            or any(not value.strip() for value in self.observation_ids)
        ):
            raise ValueError("ID observasi harus unik, tidak kosong, dan berpasangan satu per residual.")
        if any(abs(value) > 1e12 for value in self.observations_tco2e):
            raise ValueError("Observasi ketidakpastian di luar rentang yang didukung.")
        return self


class UncertaintyCorrelation(Strict):
    source_a: Literal["model", "field_validation", "sampling_design", "spatial_variation", "emission_factor", "leakage"]
    source_b: Literal["model", "field_validation", "sampling_design", "spatial_variation", "emission_factor", "leakage"]
    correlation: float = Field(ge=-1, le=1)
    evidence: list[str] = Field(min_length=1, max_length=10)


class UncertaintyBudgetInput(Strict):
    central_net_tco2e: float = Field(ge=-1e12, le=1e12)
    confidence_level_pct: float = Field(default=90, gt=50, lt=100)
    components: list[UncertaintyComponent] = Field(min_length=1, max_length=6)
    correlation_note: str = Field(default="Independen kecuali ada bukti korelasi.", max_length=1000)
    correlations: list[UncertaintyCorrelation] = Field(default_factory=list, max_length=15)
    simulation_draws: int = Field(default=20000, ge=1000, le=100000)
    seed: int = Field(default=20261124, ge=0, le=2147483647)
    evidence: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def all_sources_once(self):
        if len({item.source for item in self.components}) != len(self.components):
            raise ValueError("Setiap sumber ketidakpastian hanya boleh dimasukkan sekali.")
        sources = {item.source for item in self.components}
        pairs = set()
        for item in self.correlations:
            pair = tuple(sorted((item.source_a, item.source_b)))
            if item.source_a == item.source_b or pair in pairs:
                raise ValueError("Korelasi harus unik dan hanya boleh antar dua sumber berbeda.")
            if not set(pair).issubset(sources):
                raise ValueError("Korelasi hanya dapat menghubungkan sumber yang ada di anggaran.")
            pairs.add(pair)
        return self


class BlueCarbonPeriod(Strict):
    year: int = Field(ge=1900, le=2100)
    habitat: Literal["mangrove", "tidal_marsh", "seagrass"]
    habitat_area_ha: float = Field(ge=0, le=1e7)
    biomass_mg_c_ha: float = Field(ge=0, le=1e7)
    sediment_soc_mg_c_ha: float = Field(ge=0, le=1e7)
    habitat_area_se_ha: float | None = Field(default=None, ge=0, le=1e7)
    biomass_se_mg_c_ha: float | None = Field(default=None, ge=0, le=1e7)
    sediment_soc_se_mg_c_ha: float | None = Field(default=None, ge=0, le=1e7)
    biomass_soc_correlation: float = Field(default=0, ge=-1, le=1)
    erosion_area_ha: float = Field(default=0, ge=0, le=1e7)
    erosion_area_basis: Literal["period", "cumulative"] = "period"
    submergence_area_ha: float = Field(default=0, ge=0, le=1e7)
    ch4_emissions_tco2e_yr: float | None = Field(default=None, ge=0, le=1e12)
    n2o_emissions_tco2e_yr: float | None = Field(default=None, ge=0, le=1e12)
    relative_sea_level_rise_mm_yr: float | None = Field(default=None, ge=-100, le=500)
    baseline_habitat_area_ha: float | None = Field(default=None, ge=0, le=1e7)
    baseline_biomass_mg_c_ha: float | None = Field(default=None, ge=0, le=1e7)
    baseline_sediment_soc_mg_c_ha: float | None = Field(default=None, ge=0, le=1e7)
    baseline_ch4_emissions_tco2e_yr: float | None = Field(default=None, ge=0, le=1e12)
    baseline_n2o_emissions_tco2e_yr: float | None = Field(default=None, ge=0, le=1e12)
    erosion_driver: Literal["sea_level_rise", "other", "unknown"] = "unknown"
    eroded_soil_carbon_t_c_ha: float | None = Field(default=None, ge=0, le=1e7)
    eroded_carbon_oxidized_pct: float | None = Field(default=None, ge=0, le=100)
    sea_level_risk_buffer_contribution_tco2e: float | None = Field(default=None, ge=0, le=1e12)
    source: str = Field(min_length=1, max_length=200)
    evidence: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def erosion_within_habitat(self):
        if self.erosion_area_ha + self.submergence_area_ha > self.habitat_area_ha:
            raise ValueError("Area erosi dan submergence tidak boleh melebihi luas habitat.")
        if self.erosion_area_ha > 0 and self.erosion_driver == "unknown":
            raise ValueError("Pilih penyebab erosi sebelum menghitung estimasi atau risiko.")
        baseline_values = (
            self.baseline_habitat_area_ha, self.baseline_biomass_mg_c_ha,
            self.baseline_sediment_soc_mg_c_ha, self.baseline_ch4_emissions_tco2e_yr,
            self.baseline_n2o_emissions_tco2e_yr,
        )
        if any(value is not None for value in baseline_values) and any(value is None for value in baseline_values):
            raise ValueError("Skenario baseline blue carbon harus mengisi luas, biomassa, SOC, CH4, dan N2O bersama-sama.")
        if (self.eroded_soil_carbon_t_c_ha is None) != (self.eroded_carbon_oxidized_pct is None):
            raise ValueError("Estimasi karbon tanah tererosi dan persentase oksidasi harus diisi bersama.")
        if self.erosion_area_ha > 0 and self.erosion_driver == "other" and self.eroded_soil_carbon_t_c_ha is None:
            raise ValueError("Untuk erosi non-kenaikan muka laut, masukkan estimasi karbon tanah tererosi dan fraksi oksidasinya.")
        if self.erosion_area_ha > 0 and self.erosion_driver == "sea_level_rise" and self.sea_level_risk_buffer_contribution_tco2e is None:
            raise ValueError("Erosi akibat kenaikan muka laut memerlukan hasil kontribusi buffer AFOLU NPRT.")
        return self


class BlueCarbonMonitoringInput(Strict):
    periods: list[BlueCarbonPeriod] = Field(min_length=2, max_length=50)
    uncertainty_pct: float | None = Field(default=None, ge=0, le=100)
    temporal_correlation: float = Field(default=0, ge=-1, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def ordered_unique_periods(self):
        years = [item.year for item in self.periods]
        if years != sorted(set(years)):
            raise ValueError("Tahun monitoring harus unik dan berurutan.")
        if len({item.habitat for item in self.periods}) != 1:
            raise ValueError("Satu rangkaian monitoring harus memakai jenis habitat yang sama.")
        baseline_supplied = [item.baseline_habitat_area_ha is not None for item in self.periods]
        if any(baseline_supplied) and not all(baseline_supplied):
            raise ValueError("Baseline harus tersedia untuk setiap periode, atau tidak dimasukkan sama sekali.")
        return self


class LeakageCommodityInput(Strict):
    commodity: str = Field(min_length=1, max_length=120)
    unit: str = Field(min_length=1, max_length=40)
    commodity_type: Literal["agricultural", "fuelwood"] = "agricultural"
    introduced_by_project: bool = False
    national_production_increasing_evidence: list[str] = Field(default_factory=list, max_length=10)
    complete_crop_rotation_years: int | None = Field(default=None, ge=1, le=100)
    historical_project_production: list[float] = Field(default_factory=list, max_length=100)
    monitored_project_production: float = Field(ge=0, le=1e15)
    annual_yield_growth_pct: float = Field(default=0, ge=-100, le=1000)
    yield_on_new_land_units_per_ha: float = Field(gt=0, le=1e15)
    increased_supply_pct: float | None = Field(default=None, ge=0, le=100)
    new_land_pct: float | None = Field(default=None, ge=0, le=100)
    mitigation_historical_production: list[float] = Field(default_factory=list, max_length=100)
    mitigation_crop_rotation_years: int | None = Field(default=None, ge=1, le=100)
    mitigation_monitored_production: float | None = Field(default=None, ge=0, le=1e15)
    mitigation_fuelwood_new_plantation: bool = False
    evidence: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_production_records(self):
        if any(value < 0 or value > 1e15 for value in self.historical_project_production + self.mitigation_historical_production):
            raise ValueError("Produksi historis harus berada pada rentang 0 sampai 1e15.")
        if self.introduced_by_project:
            if self.commodity_type != "agricultural":
                raise ValueError("Komoditas baru dalam cross-commodity harus berupa komoditas pertanian.")
            if self.historical_project_production:
                raise ValueError("Komoditas baru memiliki baseline historis nol; jangan masukkan riwayat sebelum proyek.")
            if not self.national_production_increasing_evidence:
                raise ValueError("Cross-commodity baru memerlukan bukti tren produksi nasional yang meningkat.")
        elif len(self.historical_project_production) < max(3, self.complete_crop_rotation_years or 0):
            raise ValueError("Periode referensi harus mencakup setidaknya tiga tahun atau satu rotasi tanaman penuh, mana yang lebih lama.")
        if bool(self.mitigation_historical_production) != (self.mitigation_monitored_production is not None):
            valid_new_fuelwood = self.commodity_type == "fuelwood" and self.mitigation_fuelwood_new_plantation and not self.mitigation_historical_production and self.mitigation_monitored_production is not None
            if not valid_new_fuelwood:
                raise ValueError("Data produksi historis dan termonitor area mitigasi harus diisi bersama.")
        if self.mitigation_monitored_production is not None and self.commodity_type == "fuelwood" and not self.mitigation_fuelwood_new_plantation:
            raise ValueError("Mitigasi produksi fuelwood hanya dapat memakai perkebunan pohon baru.")
        if self.mitigation_fuelwood_new_plantation and self.commodity_type != "fuelwood":
            raise ValueError("Tandai perkebunan baru hanya untuk komoditas fuelwood.")
        if self.mitigation_historical_production and len(self.mitigation_historical_production) < 3:
            raise ValueError("Area mitigasi memerlukan minimal tiga tahun data historis atau satu rotasi tanaman lengkap.")
        if self.mitigation_historical_production and len(self.mitigation_historical_production) < max(3, self.mitigation_crop_rotation_years or 0):
            raise ValueError("Riwayat area mitigasi harus mencakup setidaknya tiga tahun atau satu rotasi tanaman penuh, mana yang lebih lama.")
        return self


class VMD0054LeakageInput(Strict):
    methodology_id: Literal["VM0047"] = "VM0047"
    methodology_version: Literal["1.1"] = "1.1"
    vmd0054_version: Literal["1.1"] = "1.1"
    project_start_date: dt.date
    monitoring_year: int = Field(ge=1, le=100)
    cross_commodity_accounting: bool = False
    carbon_stock_loss_t_c_ha: float = Field(ge=0, le=1e7)
    mitigation_emissions_tco2e_cumulative: float = Field(default=0, ge=0, le=1e12)
    commodities: list[LeakageCommodityInput] = Field(min_length=1, max_length=100)
    evidence: list[str] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def unique_commodities(self):
        if len({item.commodity.casefold() for item in self.commodities}) != len(self.commodities):
            raise ValueError("Setiap komoditas hanya boleh muncul sekali dalam satu tahun monitoring.")
        if any(item.introduced_by_project for item in self.commodities) and not self.cross_commodity_accounting:
            raise ValueError("Komoditas baru hanya dapat dihitung saat cross-commodity accounting diaktifkan.")
        return self


class SamplingStratum(Strict):
    name: str = Field(min_length=1, max_length=100)
    area_ha: float = Field(gt=0, le=1e7)
    expected_cv_pct: float = Field(gt=0, le=500)
    geometry: dict | None = None


class SamplingPlanInput(Strict):
    target_precision_pct: float = Field(default=10, gt=0, le=50)
    confidence_level_pct: float = Field(default=95, gt=50, lt=100)
    revisit_interval_months: int = Field(default=12, ge=3, le=120)
    plot_area_m2: float = Field(default=400, gt=0, le=10000)
    monitoring_period_id: str | None = Field(default=None, min_length=1, max_length=36)
    seed: int = Field(default=20261124, ge=0, le=2147483647)
    strata: list[SamplingStratum] = Field(min_length=1, max_length=100)
    pools: list[Literal["AGB", "BGB", "SOC", "litter", "deadwood"]] = Field(min_length=1, max_length=5)
    evidence: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def unique_strata_and_pools(self):
        if len({item.name for item in self.strata}) != len(self.strata):
            raise ValueError("Nama strata harus unik.")
        if len(set(self.pools)) != len(self.pools):
            raise ValueError("Pool tidak boleh berulang.")
        return self


class RegistryExternalRow(Strict):
    external_project_id: str = Field(min_length=1, max_length=100)
    vintage: int = Field(ge=1900, le=2100)
    serial: str = Field(min_length=1, max_length=160)
    event_type: Literal["issuance", "transfer", "use", "retirement", "cancellation"]
    event_date: dt.date
    quantity: int = Field(ge=1, le=9007199254740991)
    account: str = Field(min_length=1, max_length=200)
    transaction_id: str = Field(min_length=1, max_length=160)


class RegistryReconciliationInput(Strict):
    registry: str = Field(min_length=1, max_length=100)
    source_url: HttpUrl | None = None
    imported_at: dt.date = Field(default_factory=lambda: dt.datetime.now(dt.UTC).date())
    source_file_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    rows: list[RegistryExternalRow] = Field(min_length=1, max_length=1000)
    evidence: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("source_file_sha256")
    @classmethod
    def valid_source_hash(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("Checksum CSV harus berupa SHA-256 heksadesimal.")
        return value

    @model_validator(mode="after")
    def unique_external_ids(self):
        if len({row.transaction_id for row in self.rows}) != len(self.rows):
            raise ValueError("ID transaksi eksternal harus unik dalam satu impor.")
        if self.imported_at > dt.datetime.now(dt.UTC).date():
            raise ValueError("Tanggal impor tidak boleh di masa depan.")
        return self


class RegistrySyncInput(Strict):
    registry: Literal["Verra Registry", "UNFCCC Article 6.4 Registry"]
    external_project_id: str = Field(min_length=1, max_length=100)
    record_key: str = Field(min_length=1, max_length=100)


class SafeguardIndicatorInput(Strict):
    period: str = Field(min_length=4, max_length=32)
    indicator: Literal["fpic_sessions", "households_consulted", "grievances_resolved", "women_benefit_share_pct", "local_jobs", "biodiversity_species", "habitat_condition"]
    value: float = Field(ge=0, le=1e12)
    unit: str = Field(min_length=1, max_length=40)
    target: float | None = Field(default=None, ge=0, le=1e12)
    methodology: str = Field(default="", max_length=1000)
    evidence: list[str] = Field(min_length=1, max_length=30)
    owner: str = Field(default="", max_length=100)
    field_capture_ids: list[str] = Field(default_factory=list, max_length=100)


class CommunityBenefitInput(Strict):
    period: str = Field(min_length=4, max_length=32)
    community_or_group: str = Field(min_length=1, max_length=200)
    benefit_type: Literal["cash", "in_kind", "services", "jobs", "revenue_share"]
    value: float = Field(ge=0, le=1e12)
    currency_or_unit: str = Field(min_length=1, max_length=20)
    status: Literal["planned", "approved", "disbursed"]
    consent_reference: str = Field(default="", max_length=1000)
    evidence: list[str] = Field(min_length=1, max_length=30)
    reviewer_note: str = Field(default="", max_length=2000)
    field_capture_ids: list[str] = Field(default_factory=list, max_length=100)
    delivery_reference: str = Field(default="", max_length=1000)
    recipient_acknowledged: bool = False
    beneficiary_count: int = Field(default=0, ge=0, le=10000000)

    @model_validator(mode="after")
    def disbursement_evidence(self):
        if self.status == "disbursed" and (not self.delivery_reference.strip() or not self.recipient_acknowledged):
            raise ValueError("Manfaat berstatus disbursed memerlukan referensi bukti penyaluran dan konfirmasi penerima.")
        if self.status in {"approved", "disbursed"} and not self.consent_reference.strip():
            raise ValueError("Status approved/disbursed memerlukan referensi persetujuan masyarakat.")
        return self
