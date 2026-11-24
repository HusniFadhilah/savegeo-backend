"""Resumable administrative green-cover ranking with PostgreSQL annual snapshots.

SQLite remains a temporary compatibility store for active runs. Completed results
are published to PostgreSQL by year, period, hierarchy and parent-region scope.
"""

import hashlib
import json
import sqlite3
import time
import uuid
import math
import csv
import io
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, date, timedelta
from calendar import monthrange

import ee
from sqlalchemy import Integer, and_, case, func, or_, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models.green_ranking import GreenRankingSnapshot, GreenRankingSnapshotRow
from app.db.session import SessionLocal
from app.services.gee_common import AnalysisError, create_geometry_from_payload

LEVELS = ("province", "city", "district", "village")


@contextmanager
def connection():
    root = get_settings().upload_path / "green-rankings"
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "rankings.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, owner TEXT, config TEXT, stack TEXT, done INTEGER DEFAULT 0, lease REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS rows(run TEXT, code TEXT, payload TEXT, eligible INTEGER, green REAL, area REAL, valid_area REAL, green_area REAL, PRIMARY KEY(run,code));
        CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY, payload TEXT);
        CREATE INDEX IF NOT EXISTS ranked ON rows(run,eligible,green DESC);
    """)
    try:
        yield db
    finally:
        db.close()


def ranking_config(settings, year, start_month, end_month, *, force_refresh=False):
    level = settings.get("level", "province")
    if level not in LEVELS:
        raise AnalysisError("Tingkat peringkat tidak valid.", 422)
    year = int(year)
    start_month = int(start_month)
    end_month = int(end_month)
    if not 2000 <= year <= datetime.now(UTC).year or not 1 <= start_month <= end_month <= 12:
        raise AnalysisError("Periode peringkat tidak valid.", 422)
    land_cover_source = "GLC_FCS30D" if year <= 2022 else "Dynamic_World"
    if land_cover_source == "GLC_FCS30D" and (start_month, end_month) != (1, 12):
        raise AnalysisError("GLC_FCS30D hanya menyediakan klasifikasi tahunan; pilih bulan Januari hingga Desember.", 422)
    config = {
        "level": level,
        "year": year,
        "land_cover_source": land_cover_source,
        "start_month": start_month,
        "end_month": end_month,
        "scale": {"province": 100, "city": 30, "district": 20, "village": 10}[level],
        # v4 adds historical GLC_FCS30D support and per-pool provenance.
        # Keep the result separate from snapshots calculated with earlier methods.
        "algorithm": 4,
        "observed_through": min(
            date(year, end_month, monthrange(year, end_month)[1]), datetime.now(UTC).date()
        ).isoformat(),
        "force_refresh": bool(force_refresh),
    }
    for parent in LEVELS[: LEVELS.index(level)]:
        if settings.get(parent):
            code = str(settings[parent])
            if len(code) > 40 or not all(char.isdigit() or char in ".-_" for char in code):
                raise AnalysisError("Kode filter wilayah tidak valid.", 422)
            config[parent] = code
    return config


def snapshot_scope_key(config):
    """Stable cache identity for an annual or seasonal administrative ranking."""
    keys = ("level", "year", "start_month", "end_month", "scale", "algorithm", "land_cover_source", *LEVELS[:-1])
    identity = {key: config[key] for key in keys if key in config}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def snapshot_page(
    scope_key,
    page=1,
    page_size=50,
    search="",
    sort_by="rank",
    sort_dir="asc",
    filter_config=None,
):
    """Read a saved ranking, optionally slicing a national snapshot by parent codes."""
    db: Session = SessionLocal()
    try:
        snapshot = db.get(GreenRankingSnapshot, scope_key)
        if snapshot is None:
            raise AnalysisError("Snapshot peringkat wilayah tidak ditemukan.", 404)
        stored_config = snapshot.config or {}
        requested_config = dict(filter_config or stored_config)
        identity_keys = ("level", "year", "start_month", "end_month", "scale", "algorithm", "land_cover_source")
        if filter_config and any(
            requested_config.get(key) != stored_config.get(key) for key in identity_keys
        ):
            raise AnalysisError("Snapshot tidak sesuai dengan tahun, periode, atau tingkat wilayah.", 404)
        for key in LEVELS[:-1]:
            stored_parent = stored_config.get(key)
            requested_parent = requested_config.get(key)
            if stored_parent and stored_parent != requested_parent:
                raise AnalysisError("Snapshot tidak mencakup filter wilayah yang diminta.", 404)

        page = max(1, int(page))
        page_size = max(10, min(100, int(page_size)))
        search = str(search or "").strip()[:100]
        sort_dir = "desc" if str(sort_dir).lower() == "desc" else "asc"
        parent_filters = {
            key: str(requested_config[key])
            for key in LEVELS[:-1]
            if requested_config.get(key)
        }
        reused_national = any(not stored_config.get(key) for key in parent_filters)
        base_query = db.query(GreenRankingSnapshotRow).filter_by(scope_key=scope_key)
        for key, value in parent_filters.items():
            base_query = base_query.filter(getattr(GreenRankingSnapshotRow, key) == value)

        eligible = GreenRankingSnapshotRow.eligible.is_(True)
        green_percent = GreenRankingSnapshotRow.green_percent
        aggregate = base_query.with_entities(
            func.count(GreenRankingSnapshotRow.code).label("processed"),
            func.sum(case((eligible, 1), else_=0)).label("eligible"),
            func.sum(GreenRankingSnapshotRow.area_ha).label("area_ha"),
            func.sum(GreenRankingSnapshotRow.valid_area_ha).label("valid_area_ha"),
            func.sum(GreenRankingSnapshotRow.green_area_ha).label("green_area_ha"),
            func.avg(case((eligible, green_percent), else_=None)).label("mean_percent"),
            func.min(case((eligible, green_percent), else_=None)).label("min_percent"),
            func.max(case((eligible, green_percent), else_=None)).label("max_percent"),
            func.avg(case((eligible, green_percent * green_percent), else_=None)).label("mean_square"),
            func.sum(case((eligible, GreenRankingSnapshotRow.area_ha), else_=0)).label("ranked_area_ha"),
            func.sum(case((eligible, GreenRankingSnapshotRow.green_area_ha), else_=0)).label(
                "ranked_green_area_ha"
            ),
            func.sum(
                case((GreenRankingSnapshotRow.payload["error"].astext.is_not(None), 1), else_=0)
            ).label("failed_geometry"),
            func.sum(GreenRankingSnapshotRow.trees_ha).label("trees_ha"),
            func.sum(GreenRankingSnapshotRow.grass_ha).label("grass_ha"),
            func.sum(GreenRankingSnapshotRow.crops_ha).label("crops_ha"),
            func.sum(GreenRankingSnapshotRow.shrubs_ha).label("shrubs_ha"),
            func.sum(GreenRankingSnapshotRow.flooded_ha).label("flooded_ha"),
        ).one()
        count = int(aggregate.eligible or 0)
        eligible_query = base_query.filter(eligible, green_percent.is_not(None))
        median_values = (
            eligible_query.with_entities(green_percent)
            .order_by(green_percent.asc())
            .offset(max(0, (count - 1) // 2))
            .limit(2 if count and count % 2 == 0 else 1)
            .all()
            if count
            else []
        )
        median = (
            sum(float(value[0]) for value in median_values) / len(median_values)
            if median_values
            else None
        )
        histogram_bin = func.least(9, func.floor(green_percent / 10).cast(Integer))
        histogram_rows = (
            eligible_query.with_entities(histogram_bin, func.count())
            .group_by(histogram_bin)
            .order_by(histogram_bin)
            .all()
        )
        statistics = {
            key: (float(value) if value is not None else None)
            for key, value in {
                "area_ha": aggregate.area_ha,
                "valid_area_ha": aggregate.valid_area_ha,
                "green_area_ha": aggregate.green_area_ha,
                "mean_percent": aggregate.mean_percent,
                "min_percent": aggregate.min_percent,
                "max_percent": aggregate.max_percent,
                "mean_square": aggregate.mean_square,
                "ranked_area_ha": aggregate.ranked_area_ha,
                "ranked_green_area_ha": aggregate.ranked_green_area_ha,
                "trees_ha": aggregate.trees_ha,
                "grass_ha": aggregate.grass_ha,
                "crops_ha": aggregate.crops_ha,
                "shrubs_ha": aggregate.shrubs_ha,
                "flooded_ha": aggregate.flooded_ha,
            }.items()
        }
        statistics.update(
            processed=int(aggregate.processed or 0),
            eligible=count,
            failed_geometry=int(aggregate.failed_geometry or 0),
            median_percent=median,
            std_dev_percent=(
                math.sqrt(max(0, statistics["mean_square"] - statistics["mean_percent"] ** 2))
                if statistics["mean_percent"] is not None
                else None
            ),
            area_weighted_green_percent=(
                min(
                    100,
                    100 * (statistics["ranked_green_area_ha"] or 0)
                    / statistics["ranked_area_ha"],
                )
                if statistics["ranked_area_ha"]
                else None
            ),
            histogram=[
                {
                    "from_percent": int(bin_index) * 10,
                    "to_percent": int(bin_index) * 10 + 10,
                    "count": int(bin_count),
                }
                for bin_index, bin_count in histogram_rows
            ],
        )
        sort_columns = {
            "name": GreenRankingSnapshotRow.name,
            "code": GreenRankingSnapshotRow.code,
            "green_percent": GreenRankingSnapshotRow.green_percent,
            "coverage_percent": GreenRankingSnapshotRow.coverage_percent,
            "area_ha": GreenRankingSnapshotRow.area_ha,
            "green_area_ha": GreenRankingSnapshotRow.green_area_ha,
            "valid_area_ha": GreenRankingSnapshotRow.valid_area_ha,
            "trees_ha": GreenRankingSnapshotRow.trees_ha,
            "grass_ha": GreenRankingSnapshotRow.grass_ha,
            "crops_ha": GreenRankingSnapshotRow.crops_ha,
            "shrubs_ha": GreenRankingSnapshotRow.shrubs_ha,
            "flooded_ha": GreenRankingSnapshotRow.flooded_ha,
            "agb_mg_c_ha": GreenRankingSnapshotRow.agb_mg_c_ha,
            "bgb_mg_c_ha": GreenRankingSnapshotRow.bgb_mg_c_ha,
            "soc_mg_c_ha": GreenRankingSnapshotRow.soc_mg_c_ha,
            "litter_mg_c_ha": GreenRankingSnapshotRow.litter_mg_c_ha,
            "deadwood_mg_c_ha": GreenRankingSnapshotRow.deadwood_mg_c_ha,
        }
        if sort_by not in sort_columns and sort_by != "rank":
            sort_by = "rank"
        ranked_rows = None
        if reused_national:
            ranked_rows = (
                base_query.filter(eligible, green_percent.is_not(None))
                .with_entities(
                    GreenRankingSnapshotRow.scope_key,
                    GreenRankingSnapshotRow.code,
                    func.row_number()
                    .over(
                        order_by=(
                            green_percent.desc().nullslast(),
                            GreenRankingSnapshotRow.code.asc(),
                        )
                    )
                    .label("scoped_rank"),
                )
                .subquery()
            )
        query = base_query
        if ranked_rows is not None:
            query = query.outerjoin(
                ranked_rows,
                and_(
                    GreenRankingSnapshotRow.scope_key == ranked_rows.c.scope_key,
                    GreenRankingSnapshotRow.code == ranked_rows.c.code,
                ),
            )
        if search:
            term = f"%{search}%"
            query = query.filter(
                or_(GreenRankingSnapshotRow.name.ilike(term), GreenRankingSnapshotRow.code.ilike(term))
            )
        records_filtered = query.count()
        if sort_by == "rank":
            rank_column = (
                ranked_rows.c.scoped_rank
                if ranked_rows is not None
                else GreenRankingSnapshotRow.rank
            )
            order = rank_column.asc().nullslast()
            if sort_dir == "desc":
                order = rank_column.desc().nullslast()
            query = query.order_by(GreenRankingSnapshotRow.eligible.desc(), order, GreenRankingSnapshotRow.code.asc())
        else:
            column = sort_columns[sort_by]
            order = column.desc().nullslast() if sort_dir == "desc" else column.asc().nullslast()
            query = query.order_by(order, GreenRankingSnapshotRow.code.asc())
        if reused_national:
            stored_rows = query.with_entities(
                GreenRankingSnapshotRow, ranked_rows.c.scoped_rank
            ).offset((page - 1) * page_size).limit(page_size).all()
            rows = [
                {**row.payload, "rank": int(scoped_rank_value) if scoped_rank_value is not None else None}
                for row, scoped_rank_value in stored_rows
            ]
        else:
            stored_rows = query.offset((page - 1) * page_size).limit(page_size).all()
            rows = [{**row.payload, "rank": row.rank} for row in stored_rows]
        records_total = base_query.count()
        now = datetime.now(UTC)
        response_config = {**stored_config, **requested_config}
        return {
            "run_id": f"snapshot:{scope_key}",
            "cursor": scope_key,
            "done": True,
            "config": response_config,
            "page": page,
            "page_size": page_size,
            "records_total": records_total,
            "records_filtered": records_filtered,
            "search": search,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "rows": rows,
            "statistics": statistics,
            "status": "complete",
            "method": snapshot.method,
            "boundary_source": snapshot.boundary_source,
            "source_url": snapshot.source_url,
            "snapshot_saved_at": snapshot.refreshed_at.isoformat(),
            "snapshot_refresh_due": snapshot.refresh_due_at <= now,
            "snapshot_reused_national": reused_national,
        }
    finally:
        db.close()


def latest_snapshot(config, **page_options):
    """Prefer the closest completed scope, falling back through its ancestors."""
    parent_keys = [
        key for key in LEVELS[: LEVELS.index(config["level"])] if config.get(key)
    ]
    missing_scope_error = None
    for parent_count in range(len(parent_keys), -1, -1):
        candidate_config = {
            key: value
            for key, value in config.items()
            if key not in LEVELS[:-1] and key not in parent_keys
        }
        candidate_config.update(
            {key: config[key] for key in parent_keys[:parent_count]}
        )
        try:
            return snapshot_page(
                snapshot_scope_key(candidate_config),
                filter_config=config,
                **page_options,
            )
        except AnalysisError as exc:
            if exc.status_code != 404:
                raise
            missing_scope_error = exc
    raise missing_scope_error or AnalysisError("Snapshot peringkat wilayah tidak ditemukan.", 404)


def publish_snapshot(run, owner):
    """Atomically replace the saved annual result after a run completes."""
    with connection() as local_db:
        item = local_db.execute("SELECT * FROM runs WHERE id=? AND owner=?", (run, owner)).fetchone()
        if item is None or not item["done"]:
            return None
        run_config = json.loads(item["config"])
        scope_key = snapshot_scope_key(run_config)
        force_refresh = bool(run_config.get("force_refresh"))
        config = {key: value for key, value in run_config.items() if key != "force_refresh"}
        result = result_page(run, owner, page_size=100)
        cached_rows = local_db.execute(
            "SELECT payload FROM rows WHERE run=? ORDER BY eligible DESC,green DESC,code ASC", (run,)
        )

        db: Session = SessionLocal()
        try:
            db.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": int(scope_key[:15], 16)},
            )
            snapshot = db.get(GreenRankingSnapshot, scope_key)
            if snapshot is not None and (snapshot.source_run_id == run or not force_refresh):
                return scope_key
            now = datetime.now(UTC)
            refresh_days = 7 if config["year"] == now.year else 180
            metadata = {
                "source_run_id": run,
                "config": config,
                "statistics": result["statistics"],
                "row_count": int(result["statistics"]["processed"]),
                "method": result["method"],
                "boundary_source": result["boundary_source"],
                "source_url": result["source_url"],
                "refreshed_at": now,
                "refresh_due_at": now + timedelta(days=refresh_days),
            }
            if snapshot is None:
                snapshot = GreenRankingSnapshot(scope_key=scope_key, **metadata)
                db.add(snapshot)
            else:
                for key, value in metadata.items():
                    setattr(snapshot, key, value)
            db.flush()
            db.query(GreenRankingSnapshotRow).filter_by(scope_key=scope_key).delete(synchronize_session=False)
            db.flush()

            rank = 0
            batch = []
            for record in cached_rows:
                payload = json.loads(record["payload"])
                if payload.get("eligible"):
                    rank += 1
                    row_rank = rank
                else:
                    row_rank = None
                classes = payload.get("classes_ha") or {}
                batch.append(
                    {
                        "scope_key": scope_key,
                        "code": str(payload["code"]),
                        "name": str(payload.get("name") or payload["code"]),
                        "province": payload.get("province"),
                        "city": payload.get("city"),
                        "district": payload.get("district"),
                        "rank": row_rank,
                        "eligible": bool(payload.get("eligible")),
                        "green_percent": payload.get("green_percent"),
                        "coverage_percent": payload.get("coverage_percent"),
                        "area_ha": payload.get("area_ha"),
                        "valid_area_ha": payload.get("valid_area_ha"),
                        "green_area_ha": payload.get("green_area_ha"),
                        "trees_ha": classes.get("trees"),
                        "grass_ha": classes.get("grass"),
                        "crops_ha": classes.get("crops"),
                        "shrubs_ha": classes.get("shrubs"),
                        "flooded_ha": classes.get("flooded"),
                        "agb_mg_c_ha": payload.get("agb_mg_c_ha"),
                        "bgb_mg_c_ha": payload.get("bgb_mg_c_ha"),
                        "soc_mg_c_ha": payload.get("soc_mg_c_ha"),
                        "litter_mg_c_ha": payload.get("litter_mg_c_ha"),
                        "deadwood_mg_c_ha": payload.get("deadwood_mg_c_ha"),
                        "payload": {**payload, "rank": row_rank},
                    }
                )
                if len(batch) >= 1000:
                    db.bulk_insert_mappings(GreenRankingSnapshotRow, batch)
                    batch.clear()
            if batch:
                db.bulk_insert_mappings(GreenRankingSnapshotRow, batch)
            db.commit()
            return scope_key
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def dropdown(index, parent=None):
    from app.api.routes import regions

    payload = (
        regions.get_provinces()
        if index == 0
        else regions.get_cities(parent)
        if index == 1
        else regions.get_districts(parent)
        if index == 2
        else regions.get_villages(parent)
    )
    return sorted(regions._extract_region_dropdown_items(payload), key=lambda item: item[1])


def next_region(stack, config):
    """Depth-first cursor bounded by one administrative branch, not 80k village geometries."""
    target = LEVELS.index(config["level"])
    while stack:
        frame = stack[-1]
        if frame["index"] >= len(frame["items"]):
            stack.pop()
            continue
        name, code = frame["items"][frame["index"]]
        frame["index"] += 1
        level = frame["level"]
        chosen = config.get(LEVELS[level])
        if chosen and chosen != code:
            continue
        parents = {**frame["parents"], LEVELS[level]: code}
        if level == target:
            return {"name": name, "code": code, **parents}
        stack.append({"level": level + 1, "items": dropdown(level + 1, code), "index": 0, "parents": parents})
    return None


def _insert(db, run, row):
    db.execute(
        "INSERT OR REPLACE INTO rows VALUES(?,?,?,?,?,?,?,?)",
        (
            run,
            row["code"],
            json.dumps(row),
            int(row.get("eligible", False)),
            row.get("green_percent"),
            row.get("area_ha", 0),
            row.get("valid_area_ha", 0),
            row.get("green_area_ha", 0),
        ),
    )


CARBON_POOL_DATASETS = {
    "agb": "SPAWN",
    "bgb": "ORNL_BGB",
    "soc": "OPENLANDMAP_SOC_STOCK_0_30CM_30M",
    "litter": "TROPICAL_LITTER_REFERENCE",
    "deadwood": "TROPICAL_DEADWOOD_REFERENCE",
}


def _carbon_pool_source_plan(year):
    """Resolve open sources, preserving source year and native resolution."""
    year = int(year)
    from app.registries.carbon_dataset_registry import OPENLANDMAP_SOC_PERIODS, CARBON_DATASET_REGISTRY

    agb_dataset = "ESA_CCI"
    agb_source_year = min(CARBON_DATASET_REGISTRY[agb_dataset]["available_years"], key=lambda candidate: abs(candidate - year))
    agb_status = "nearest_year_product" if agb_source_year != year else "year_product"
    if year == 2023:
        agb_dataset, agb_source_year, agb_status = "GEDI_L4D", 2023, "modelled_wall_to_wall"
    elif 2019 <= year <= 2025:
        agb_dataset, agb_source_year, agb_status = "GEDI_L4A_MONTHLY", year, "sparse_footprint_sample"
    elif year > 2025:
        agb_dataset, agb_source_year, agb_status = "GEDI_L4A_MONTHLY", 2025, "forward_estimate_from_latest_sample"

    plan = {
        "agb": {"dataset": agb_dataset, "source_year": agb_source_year, "status": agb_status},
        "bgb": {"dataset": "ORNL_BGB", "source_year": 2010, "estimate_year": year, "reference_year": 2010, "status": "derived_estimate"},
        "soc": {"dataset": "OPENLANDMAP_SOC_STOCK_0_30CM_30M", "source_year": None, "status": "period_model"},
        "litter": {"dataset": "TROPICAL_LITTER_REFERENCE", "source_year": 2010, "estimate_year": year, "reference_year": 2010, "status": "derived_proxy"},
        "deadwood": {"dataset": "TROPICAL_DEADWOOD_REFERENCE", "source_year": 2010, "estimate_year": year, "reference_year": 2010, "status": "derived_proxy"},
    }
    # Use the period containing the selected year, or the closest published
    # period outside its coverage. The source period remains in provenance.
    period_year = next(
        (key for key, (start, end) in OPENLANDMAP_SOC_PERIODS.items() if start <= year < end),
        min(OPENLANDMAP_SOC_PERIODS, key=lambda key: min(abs(year - OPENLANDMAP_SOC_PERIODS[key][0]), abs(year - OPENLANDMAP_SOC_PERIODS[key][1]))),
    )
    period_start, period_end = OPENLANDMAP_SOC_PERIODS[period_year]
    plan["soc"].update(
        source_year=period_year,
        source_period=f"{period_start}–{period_end}",
        status="period_model" if period_start <= year < period_end else "period_extrapolation",
    )
    return plan


def _carbon_pool_band_stack(features, year):
    """Build area-weighted stocks and return the exact source plan for provenance."""
    from app.registries.carbon_dataset_registry import CARBON_DATASET_REGISTRY, load_carbon_reference_ee

    roi = ee.FeatureCollection(features).geometry()
    pixel_area_ha = ee.Image.pixelArea().divide(10000)
    source_plan = _carbon_pool_source_plan(year)
    agb_source = source_plan["agb"]
    agb_image = load_carbon_reference_ee(agb_source["dataset"], agb_source["source_year"], roi=roi).select("agb")
    baseline_agb = load_carbon_reference_ee("SPAWN", 2010, roi=roi).select("agb")
    baseline_bgb = load_carbon_reference_ee("ORNL_BGB", 2010, roi=roi).select("agb")
    safe_baseline_agb = baseline_agb.updateMask(baseline_agb.gt(0))
    from app.services.detritus_reference import reference_image

    pool_images = {
        "agb": agb_image,
        "bgb": agb_image.multiply(baseline_bgb.divide(safe_baseline_agb)).rename("agb"),
        "soc": load_carbon_reference_ee(source_plan["soc"]["dataset"], source_plan["soc"]["source_year"], roi=roi).select("agb"),
        "litter": agb_image.multiply(reference_image("litter", ee).divide(safe_baseline_agb)).rename("agb"),
        "deadwood": agb_image.multiply(reference_image("deadwood", ee).divide(safe_baseline_agb)).rename("agb"),
    }
    bands = None
    for pool, source in source_plan.items():
        density = pool_images[pool]
        valid_area = pixel_area_ha.updateMask(density.mask()).rename(f"{pool}_valid_area_ha")
        total = density.multiply(pixel_area_ha).rename(f"{pool}_total_mg_c")
        pool_bands = valid_area.addBands(total)
        bands = pool_bands if bands is None else bands.addBands(pool_bands)
        dataset = source["dataset"]
        meta = CARBON_DATASET_REGISTRY.get(dataset)
        if meta is None:
            raise AnalysisError(f"Metadata sumber karbon tidak ditemukan: {dataset}.", 500)
        if pool in {"bgb", "litter", "deadwood"}:
            source["formula"] = "AGB(year) × source_pool_2010 / AGB_2010"
            source["resolution_m"] = max(
                int(source_plan["agb"].get("resolution_m") or 0),
                int(meta.get("resolution") or 0),
            ) or None
            source["name"] = meta.get("name", dataset)
            source["source_url"] = meta.get("source_url") or meta.get("gee_id")
            source["coverage_type"] = source_plan["agb"].get("coverage_type", "wall_to_wall")
            source["method"] = meta.get("description", "")
            continue
        source.update(
            name=meta.get("name", dataset),
            resolution_m=meta.get("resolution"),
            source_url=meta.get("source_url") or meta.get("gee_id"),
            coverage_type=("sampled_footprints" if dataset == "GEDI_L4A_MONTHLY" else "wall_to_wall"),
            method=meta.get("description", ""),
        )
    return bands, source_plan


def _carbon_pool_values(properties, source_plan):
    """Convert zonal sums and attach source vintage/resolution to every pool."""
    values = {}
    for pool in CARBON_POOL_DATASETS:
        valid_area = properties.get(f"{pool}_valid_area_ha")
        total = properties.get(f"{pool}_total_mg_c")
        valid_area = float(valid_area) if valid_area is not None else 0.0
        total = float(total) if total is not None else 0.0
        values[f"{pool}_mg_c_ha"] = total / valid_area if valid_area > 0 else None
        values[f"{pool}_total_mg_c"] = total if valid_area > 0 else None
        values.setdefault("carbon_pool_sources", {})[pool] = {
            **source_plan[pool],
            "valid_area_ha": valid_area,
            "valid_coverage_percent": None,
        }
    return values


def result_page(
    run, owner, page=1, page_size=50, search="", sort_by="rank", sort_dir="asc", filter_config=None
):
    if run.startswith("snapshot:"):
        return snapshot_page(
            run.split(":", 1)[1],
            page=page,
            page_size=page_size,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
            filter_config=filter_config,
        )
    with connection() as db:
        item = db.execute("SELECT * FROM runs WHERE id=? AND owner=?", (run, owner)).fetchone()
        if item is None:
            raise AnalysisError("Peringkat wilayah tidak ditemukan untuk akun ini.", 404)
        page = max(1, int(page))
        page_size = max(10, min(100, int(page_size)))
        search = str(search or "").strip()[:100]
        sort_dir = "desc" if str(sort_dir).lower() == "desc" else "asc"
        sort_columns = {
            "name": "name_sort",
            "code": "code",
            "green_percent": "green",
            "coverage_percent": "coverage_sort",
            "area_ha": "area",
            "green_area_ha": "green_area",
            "valid_area_ha": "valid_area",
            "trees_ha": "trees_sort",
            "grass_ha": "grass_sort",
            "crops_ha": "crops_sort",
            "shrubs_ha": "shrubs_sort",
            "flooded_ha": "flooded_sort",
            "agb_mg_c_ha": "agb_sort",
            "bgb_mg_c_ha": "bgb_sort",
            "soc_mg_c_ha": "soc_sort",
            "litter_mg_c_ha": "litter_sort",
            "deadwood_mg_c_ha": "deadwood_sort",
        }
        if sort_by not in sort_columns and sort_by != "rank":
            sort_by = "rank"
        summary = dict(
            db.execute(
                """SELECT COUNT(*) AS processed, SUM(eligible) AS eligible,
            SUM(area) AS area_ha, SUM(valid_area) AS valid_area_ha, SUM(green_area) AS green_area_ha,
            AVG(CASE WHEN eligible=1 THEN green END) AS mean_percent,
            MIN(CASE WHEN eligible=1 THEN green END) AS min_percent,
            MAX(CASE WHEN eligible=1 THEN green END) AS max_percent,
            AVG(CASE WHEN eligible=1 THEN green*green END) AS mean_square,
            SUM(CASE WHEN eligible=1 THEN area END) AS ranked_area_ha,
            SUM(CASE WHEN eligible=1 THEN green_area END) AS ranked_green_area_ha,
            SUM(CASE WHEN json_extract(payload,'$.error') IS NOT NULL THEN 1 ELSE 0 END) AS failed_geometry,
            SUM(json_extract(payload,'$.classes_ha.trees')) AS trees_ha,
            SUM(json_extract(payload,'$.classes_ha.grass')) AS grass_ha,
            SUM(json_extract(payload,'$.classes_ha.crops')) AS crops_ha,
            SUM(json_extract(payload,'$.classes_ha.shrubs')) AS shrubs_ha,
            SUM(json_extract(payload,'$.classes_ha.flooded')) AS flooded_ha FROM rows WHERE run=?""",
                (run,),
            ).fetchone()
        )
        records_total = int(db.execute("SELECT COUNT(*) FROM rows WHERE run=?", (run,)).fetchone()[0])
        pattern = f"%{search.lower()}%" if search else ""
        records_filtered = records_total
        if search:
            records_filtered = int(
                db.execute(
                    "SELECT COUNT(*) FROM rows WHERE run=? AND (lower(COALESCE(json_extract(payload,'$.name'),'')) LIKE ? OR lower(code) LIKE ?)",
                    (run, pattern, pattern),
                ).fetchone()[0]
            )
        query_params = [
            run,
            int(bool(search)),
            pattern,
            pattern,
            sort_by,
            sort_dir,
            sort_by,
            sort_dir,
            sort_by,
            page_size,
            (page - 1) * page_size,
        ]
        rows = []
        for item_row in db.execute(
            """WITH ranked AS (
                SELECT code, payload, eligible, green, area, valid_area, green_area,
                    json_extract(payload,'$.name') AS name_sort,
                    json_extract(payload,'$.coverage_percent') AS coverage_sort,
                    json_extract(payload,'$.classes_ha.trees') AS trees_sort,
                    json_extract(payload,'$.classes_ha.grass') AS grass_sort,
                    json_extract(payload,'$.classes_ha.crops') AS crops_sort,
                    json_extract(payload,'$.classes_ha.shrubs') AS shrubs_sort,
                    json_extract(payload,'$.classes_ha.flooded') AS flooded_sort,
                    json_extract(payload,'$.agb_mg_c_ha') AS agb_sort,
                    json_extract(payload,'$.bgb_mg_c_ha') AS bgb_sort,
                    json_extract(payload,'$.soc_mg_c_ha') AS soc_sort,
                    json_extract(payload,'$.litter_mg_c_ha') AS litter_sort,
                    json_extract(payload,'$.deadwood_mg_c_ha') AS deadwood_sort,
                    CASE WHEN eligible=1 THEN ROW_NUMBER() OVER (PARTITION BY eligible ORDER BY green DESC, code ASC) END AS official_rank
                FROM rows WHERE run=?
            )
            SELECT payload, official_rank FROM ranked
            WHERE (? = 0 OR lower(COALESCE(name_sort,'')) LIKE ? OR lower(code) LIKE ?)
            ORDER BY eligible DESC,
                CASE WHEN CASE ?
                    WHEN 'rank' THEN official_rank
                    WHEN 'name' THEN name_sort
                    WHEN 'code' THEN code
                    WHEN 'green_percent' THEN green
                    WHEN 'coverage_percent' THEN coverage_sort
                    WHEN 'area_ha' THEN area
                    WHEN 'green_area_ha' THEN green_area
                    WHEN 'valid_area_ha' THEN valid_area
                    WHEN 'trees_ha' THEN trees_sort
                    WHEN 'grass_ha' THEN grass_sort
                    WHEN 'crops_ha' THEN crops_sort
                    WHEN 'shrubs_ha' THEN shrubs_sort
                    WHEN 'flooded_ha' THEN flooded_sort
                    WHEN 'agb_mg_c_ha' THEN agb_sort
                    WHEN 'bgb_mg_c_ha' THEN bgb_sort
                    WHEN 'soc_mg_c_ha' THEN soc_sort
                    WHEN 'litter_mg_c_ha' THEN litter_sort
                    WHEN 'deadwood_mg_c_ha' THEN deadwood_sort
                END IS NULL THEN 1 ELSE 0 END ASC,
                CASE WHEN ? = 'asc' THEN CASE ?
                    WHEN 'rank' THEN official_rank
                    WHEN 'name' THEN name_sort
                    WHEN 'code' THEN code
                    WHEN 'green_percent' THEN green
                    WHEN 'coverage_percent' THEN coverage_sort
                    WHEN 'area_ha' THEN area
                    WHEN 'green_area_ha' THEN green_area
                    WHEN 'valid_area_ha' THEN valid_area
                    WHEN 'trees_ha' THEN trees_sort
                    WHEN 'grass_ha' THEN grass_sort
                    WHEN 'crops_ha' THEN crops_sort
                    WHEN 'shrubs_ha' THEN shrubs_sort
                    WHEN 'flooded_ha' THEN flooded_sort
                    WHEN 'agb_mg_c_ha' THEN agb_sort
                    WHEN 'bgb_mg_c_ha' THEN bgb_sort
                    WHEN 'soc_mg_c_ha' THEN soc_sort
                    WHEN 'litter_mg_c_ha' THEN litter_sort
                    WHEN 'deadwood_mg_c_ha' THEN deadwood_sort
                END END ASC,
                CASE WHEN ? = 'desc' THEN CASE ?
                    WHEN 'rank' THEN official_rank
                    WHEN 'name' THEN name_sort
                    WHEN 'code' THEN code
                    WHEN 'green_percent' THEN green
                    WHEN 'coverage_percent' THEN coverage_sort
                    WHEN 'area_ha' THEN area
                    WHEN 'green_area_ha' THEN green_area
                    WHEN 'valid_area_ha' THEN valid_area
                    WHEN 'trees_ha' THEN trees_sort
                    WHEN 'grass_ha' THEN grass_sort
                    WHEN 'crops_ha' THEN crops_sort
                    WHEN 'shrubs_ha' THEN shrubs_sort
                    WHEN 'flooded_ha' THEN flooded_sort
                    WHEN 'agb_mg_c_ha' THEN agb_sort
                    WHEN 'bgb_mg_c_ha' THEN bgb_sort
                    WHEN 'soc_mg_c_ha' THEN soc_sort
                    WHEN 'litter_mg_c_ha' THEN litter_sort
                    WHEN 'deadwood_mg_c_ha' THEN deadwood_sort
                END END DESC,
                code ASC
            LIMIT ? OFFSET ?""",
            query_params,
        ):
            row = json.loads(item_row["payload"])
            row["rank"] = int(item_row["official_rank"]) if item_row["official_rank"] is not None else None
            rows.append(row)
        summary["area_weighted_green_percent"] = (
            min(100, 100 * (summary["ranked_green_area_ha"] or 0) / summary["ranked_area_ha"])
            if summary["ranked_area_ha"]
            else None
        )
        summary["std_dev_percent"] = (
            math.sqrt(max(0, summary["mean_square"] - summary["mean_percent"] ** 2))
            if summary["mean_percent"] is not None
            else None
        )
        count = int(summary["eligible"] or 0)
        medians = (
            [
                row[0]
                for row in db.execute(
                    "SELECT green FROM rows WHERE run=? AND eligible=1 ORDER BY green LIMIT ? OFFSET ?",
                    (run, 2 if count % 2 == 0 else 1, max(0, (count - 1) // 2)),
                )
            ]
            if count
            else []
        )
        summary["median_percent"] = sum(medians) / len(medians) if medians else None
        summary["histogram"] = [
            {"from_percent": row[0] * 10, "to_percent": row[0] * 10 + 10, "count": row[1]}
            for row in db.execute(
                "SELECT MIN(9,CAST(green/10 AS INTEGER)),COUNT(*) FROM rows WHERE run=? AND eligible=1 GROUP BY 1 ORDER BY 1",
                (run,),
            )
        ]
        return {
            "run_id": run,
            "cursor": hashlib.sha256(item["stack"].encode()).hexdigest(),
            "done": bool(item["done"]),
            "config": json.loads(item["config"]),
            "page": page,
            "page_size": page_size,
            "records_total": records_total,
            "records_filtered": records_filtered,
            "search": search,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "rows": rows,
            "statistics": summary,
            "status": "complete" if item["done"] else "partial",
            "method": (
                "GLC_FCS30D tahunan 30 m (2000–2022); kelas hutan, semak, rumput, tanaman, dan lahan basah dikelompokkan sebagai hijau. "
                if json.loads(item["config"]).get("land_cover_source") == "GLC_FCS30D"
                else "Dynamic World: mode kelas periode terpilih; hijau = pohon, rumput, vegetasi tergenang, tanaman, semak. "
            ) + "Persentase memakai luas batas administratif penuh. Wilayah dengan cakupan data <80% tidak diberi peringkat. Urutan pada hasil parsial belum merupakan peringkat nasional lengkap.",
            "boundary_source": "Penyedia batas administrasi SAVEGEO; kode hierarki dan geometri per wilayah dipertahankan.",
            "source_url": (
                "https://essd.copernicus.org/articles/16/1353/2024/"
                if json.loads(item["config"]).get("land_cover_source") == "GLC_FCS30D"
                else "https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1"
            ),
        }


def run_batch(data):
    owner = str(data.get("_request_owner") or "")
    if not owner:
        raise AnalysisError("Jalankan peringkat melalui pekerjaan analisis terautentikasi.", 403)
    settings = data.get("extension_settings") or {}
    run = str(settings.get("run_id") or "")
    if run.startswith("snapshot:"):
        config = ranking_config(
            settings,
            data.get("year", datetime.now(UTC).year),
            data.get("start_month", 1),
            data.get("end_month", 12),
        )
        return snapshot_page(run.split(":", 1)[1], filter_config=config)
    config = None
    if not run:
        config = ranking_config(
            settings,
            data.get("year", datetime.now(UTC).year),
            data.get("start_month", 1),
            data.get("end_month", 12),
            force_refresh=settings.get("force_refresh", False),
        )
        if not config["force_refresh"]:
            try:
                return latest_snapshot(config)
            except AnalysisError as exc:
                if exc.status_code != 404:
                    raise
    with connection() as db:
        if not run:
            run = str(uuid.uuid4())
            stack = [{"level": 0, "items": dropdown(0), "index": 0, "parents": {}}]
            db.execute(
                "INSERT INTO runs(id,owner,config,stack) VALUES(?,?,?,?)",
                (run, owner, json.dumps(config), json.dumps(stack)),
            )
            db.commit()
            # Return the durable run identifier before the first expensive batch.
            return result_page(run, owner)
        item = db.execute("SELECT * FROM runs WHERE id=? AND owner=?", (run, owner)).fetchone()
        if item is None:
            raise AnalysisError("Pekerjaan peringkat tidak ditemukan.", 404)
        if item["done"]:
            completed_config = json.loads(item["config"])
            scope_key = snapshot_scope_key(completed_config)
            if not completed_config.get("force_refresh"):
                try:
                    return snapshot_page(scope_key)
                except AnalysisError as exc:
                    if exc.status_code != 404:
                        raise
            scope_key = publish_snapshot(run, owner)
            if scope_key:
                return snapshot_page(scope_key)
            return result_page(run, owner)
        if (
            settings.get("cursor")
            and settings["cursor"] != hashlib.sha256(item["stack"].encode()).hexdigest()
        ):
            return result_page(run, owner)
        if not db.execute(
            "UPDATE runs SET lease=? WHERE id=? AND lease<?", (time.time() + 1800, run, time.time())
        ).rowcount:
            raise AnalysisError("Batch peringkat ini masih berjalan. Tunggu sebelum melanjutkan.", 409)
        db.commit()
        config = json.loads(item["config"])
        stack = json.loads(item["stack"])
        try:
            candidates = []
            # Large province polygons need smaller requests than village boundaries.
            # Municipalities can be very large polygons. Grouping four in one
            # Earth Engine reduction often exceeds the bounded HTTPS timeout;
            # isolate them so one costly region does not block its neighbors.
            batch_size = {"province": 1, "city": 1, "district": 10, "village": 20}[config["level"]]
            for _ in range(batch_size):
                candidate = next_region(stack, config)
                if candidate is None:
                    break
                candidates.append(candidate)
            from app.api.routes import regions

            def geometry(candidate):
                try:
                    geo = regions.get_region_geometry(endpoint=config["level"], code=candidate["code"])
                    checksum = hashlib.sha256(json.dumps(geo, sort_keys=True).encode()).hexdigest()
                    cache_config = {
                        key: value
                        for key, value in config.items()
                        if key not in {"force_refresh", "observed_through"}
                    }
                    key = hashlib.sha256(
                        json.dumps([cache_config, candidate["code"], checksum], sort_keys=True).encode()
                    ).hexdigest()
                    return candidate, geo, key, None
                except Exception as exc:  # noqa: BLE001 - one missing boundary must not abort all national regions
                    return candidate, None, None, type(exc).__name__

            features = []
            context = {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                for candidate, geo, key, error in pool.map(geometry, candidates):
                    if error:
                        _insert(
                            db,
                            run,
                            {**candidate, "eligible": False, "error": f"Geometri tidak tersedia ({error})"},
                        )
                        continue
                    cached = (
                        None
                        if config.get("force_refresh")
                        else db.execute("SELECT payload FROM cache WHERE key=?", (key,)).fetchone()
                    )
                    if cached:
                        _insert(db, run, json.loads(cached["payload"]))
                        continue
                    try:
                        roi = create_geometry_from_payload({"geojson": geo})
                    except Exception:  # noqa: BLE001 - retain failed boundaries in coverage accounting
                        _insert(
                            db, run, {**candidate, "eligible": False, "error": "Geometri batas tidak valid."}
                        )
                        continue
                    features.append(
                        ee.Feature(
                            roi,
                            {"code": candidate["code"], "region_area": roi.area(maxError=10).divide(10000)},
                        )
                    )
                    context[candidate["code"]] = (candidate, key)
            if features:
                start = f"{config['year']}-{config['start_month']:02d}-01"
                end = (
                    f"{config['year'] + 1}-01-01"
                    if config["end_month"] == 12
                    else f"{config['year']}-{config['end_month'] + 1:02d}-01"
                )
                if config.get("land_cover_source") == "GLC_FCS30D":
                    labels = (
                        ee.ImageCollection("projects/sat-io/open-datasets/GLC-FCS30D/annual")
                        .select(f"b{config['year'] - 1999}")
                        .mosaic()
                        .clip(ee.FeatureCollection(features).geometry())
                    )
                    valid_labels = labels.gt(0)
                    classes_by_group = {
                        "green": [10, 11, 12, 20, 51, 52, 61, 62, 71, 72, 81, 82, 91, 92, 120, 121, 122, 130, 152, 153, 180, 181, 182, 183, 184, 185, 186, 187],
                        "trees": [51, 52, 61, 62, 71, 72, 81, 82, 91, 92],
                        "grass": [130, 153],
                        "crops": [10, 11, 12, 20],
                        "shrubs": [120, 121, 122, 152],
                        "flooded": [180, 181, 182, 183, 184, 185, 186, 187],
                    }
                else:
                    collection = (
                        ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
                        .filterBounds(ee.FeatureCollection(features).geometry())
                        .filterDate(start, end)
                    )
                    if not collection.size().getInfo():
                        raise AnalysisError("Tidak ada Dynamic World untuk batch dan periode ini.", 404)
                    labels = collection.select("label").mode()
                    valid_labels = labels.mask()
                    classes_by_group = {
                        "green": [1, 2, 3, 4, 5], "trees": [1], "grass": [2],
                        "crops": [4], "shrubs": [5], "flooded": [3],
                    }
                area = ee.Image.pixelArea().divide(10000).updateMask(valid_labels)
                bands = area.rename("valid")
                for name, classes in classes_by_group.items():
                    bands = bands.addBands(
                        labels.remap(classes, [1] * len(classes), 0).multiply(area).rename(name)
                    )
                reduced = (
                    bands.reduceRegions(
                        collection=ee.FeatureCollection(features),
                        reducer=ee.Reducer.sum(),
                        scale=config["scale"],
                        tileScale=4,
                    )
                    .map(lambda feature: feature.setGeometry(None))
                    .getInfo()
                )
                pool_bands, pool_source_plan = _carbon_pool_band_stack(features, config["year"])
                pool_reduced = (
                    pool_bands.reduceRegions(
                        collection=ee.FeatureCollection(features),
                        reducer=ee.Reducer.sum(),
                        # Preserve the 30 m GEDI L4D and OpenLandMap grids where selected.
                        scale=max(30, config["scale"]),
                        tileScale=4,
                    )
                    .map(lambda feature: feature.setGeometry(None))
                    .getInfo()
                )
                carbon_by_code = {
                    str(feature["properties"]["code"]): _carbon_pool_values(feature["properties"], pool_source_plan)
                    for feature in pool_reduced.get("features", [])
                }
                for feature in reduced.get("features", []):
                    props = feature["properties"]
                    candidate, key = context[str(props["code"])]
                    area_ha = float(props.get("region_area") or 0)
                    valid = float(props.get("valid") or 0)
                    green = float(props.get("green") or 0)
                    coverage = min(100, valid / area_ha * 100) if area_ha else 0
                    carbon_values = carbon_by_code.get(str(props["code"]), {})
                    for source in carbon_values.get("carbon_pool_sources", {}).values():
                        source["valid_coverage_percent"] = (
                            min(100, source["valid_area_ha"] / area_ha * 100) if area_ha else None
                        )
                        source["total_scope"] = "sampled_valid_area" if source.get("coverage_type") == "sampled_footprints" else "valid_raster_area"
                    row = {
                        **candidate,
                        "area_ha": area_ha,
                        "valid_area_ha": valid,
                        "green_area_ha": green,
                        "green_percent": min(100, green / area_ha * 100) if area_ha else None,
                        "coverage_percent": coverage,
                        "eligible": coverage >= 80,
                        "scale_m": config["scale"],
                        "classes_ha": {
                            name: props.get(name) for name in ("trees", "grass", "crops", "shrubs", "flooded")
                        },
                        **carbon_values,
                    }
                    _insert(db, run, row)
                    db.execute("INSERT OR REPLACE INTO cache VALUES(?,?)", (key, json.dumps(row)))
            db.execute(
                "UPDATE runs SET stack=?,done=?,lease=0 WHERE id=?", (json.dumps(stack), int(not stack), run)
            )
            db.commit()
        except Exception:
            db.rollback()
            db.execute("UPDATE runs SET lease=0 WHERE id=?", (run,))
            db.commit()
            raise
    result = result_page(run, owner)
    if result["done"]:
        publish_snapshot(run, owner)
        result = snapshot_page(snapshot_scope_key(result["config"]))
    return result


def snapshot_csv_rows(scope_key, filter_config=None):
    """Stream the persisted regional snapshot without loading it into memory."""
    columns = [
        "status",
        "year",
        "start_month",
        "end_month",
        "level",
        "code",
        "name",
        "province",
        "city",
        "district",
        "rank",
        "eligible",
        "area_ha",
        "valid_area_ha",
        "green_area_ha",
        "green_percent",
        "coverage_percent",
        "trees_ha",
        "grass_ha",
        "crops_ha",
        "shrubs_ha",
        "flooded_ha",
        "agb_mg_c_ha",
        "bgb_mg_c_ha",
        "soc_mg_c_ha",
        "litter_mg_c_ha",
        "deadwood_mg_c_ha",
        "agb_total_mg_c",
        "bgb_total_mg_c",
        "soc_total_mg_c",
        "litter_total_mg_c",
        "deadwood_total_mg_c",
        "error",
    ]
    meta = snapshot_page(scope_key, page_size=10, filter_config=filter_config)

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        yield "\ufeff" + buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        db: Session = SessionLocal()
        try:
            snapshot = db.get(GreenRankingSnapshot, scope_key)
            stored_config = snapshot.config if snapshot else {}
            query = db.query(GreenRankingSnapshotRow).filter_by(scope_key=scope_key)
            parent_filters = {
                key: str(filter_config[key])
                for key in LEVELS[:-1]
                if filter_config and filter_config.get(key)
            }
            for key, value in parent_filters.items():
                query = query.filter(getattr(GreenRankingSnapshotRow, key) == value)
            reused_national = any(not stored_config.get(key) for key in parent_filters)
            if reused_national:
                query = query.order_by(
                    GreenRankingSnapshotRow.eligible.desc(),
                    GreenRankingSnapshotRow.green_percent.desc().nullslast(),
                    GreenRankingSnapshotRow.code.asc(),
                )
            else:
                query = query.order_by(
                    GreenRankingSnapshotRow.eligible.desc(),
                    GreenRankingSnapshotRow.rank.asc().nullslast(),
                    GreenRankingSnapshotRow.code.asc(),
                )
            rank = 0
            query = query.yield_per(1000)
            for record in query:
                row = dict(record.payload)
                if reused_national:
                    if record.eligible:
                        rank += 1
                        row_rank = rank
                    else:
                        row_rank = ""
                else:
                    row_rank = record.rank or ""
                row.update(
                    status="complete",
                    year=meta["config"]["year"],
                    start_month=meta["config"]["start_month"],
                    end_month=meta["config"]["end_month"],
                    level=meta["config"]["level"],
                    rank=row_rank,
                )
                row.update({f"{key}_ha": value for key, value in row.get("classes_ha", {}).items()})
                values = [row.get(column, "") for column in columns]
                values = [
                    "'" + value
                    if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                    else value
                    for value in values
                ]
                writer.writerow(values)
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
        finally:
            db.close()

    return generate()


def csv_rows(run, owner, filter_config=None):
    """Export every computed region, including excluded/no-data rows and run status."""
    if run.startswith("snapshot:"):
        return snapshot_csv_rows(run.split(":", 1)[1], filter_config=filter_config)
    meta = result_page(run, owner)

    def generate():
        columns = [
            "status",
            "year",
            "start_month",
            "end_month",
            "level",
            "code",
            "name",
            "province",
            "city",
            "district",
            "rank",
            "eligible",
            "area_ha",
            "valid_area_ha",
            "green_area_ha",
            "green_percent",
            "coverage_percent",
            "trees_ha",
            "grass_ha",
            "crops_ha",
            "shrubs_ha",
            "flooded_ha",
            "agb_mg_c_ha",
            "bgb_mg_c_ha",
            "soc_mg_c_ha",
            "litter_mg_c_ha",
            "deadwood_mg_c_ha",
            "agb_total_mg_c",
            "bgb_total_mg_c",
            "soc_total_mg_c",
            "litter_total_mg_c",
            "deadwood_total_mg_c",
            "error",
        ]
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        yield "\ufeff" + buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        with connection() as db:
            for index, record in enumerate(
                db.execute(
                    "SELECT payload FROM rows WHERE run=? ORDER BY eligible DESC,green DESC,code", (run,)
                )
            ):
                row = json.loads(record["payload"])
                row.update(
                    status=meta["status"],
                    year=meta["config"]["year"],
                    start_month=meta["config"]["start_month"],
                    end_month=meta["config"]["end_month"],
                    level=meta["config"]["level"],
                    rank=index + 1 if row.get("eligible") else "",
                )
                row.update({f"{key}_ha": value for key, value in row.get("classes_ha", {}).items()})
                values = [row.get(column, "") for column in columns]
                values = [
                    "'" + value
                    if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                    else value
                    for value in values
                ]
                writer.writerow(values)
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)

    return generate()
