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
from sqlalchemy import or_, text
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
    if not 2015 <= year <= datetime.now(UTC).year or not 1 <= start_month <= end_month <= 12:
        raise AnalysisError("Periode peringkat tidak valid.", 422)
    config = {
        "level": level,
        "year": year,
        "start_month": start_month,
        "end_month": end_month,
        "scale": {"province": 100, "city": 30, "district": 20, "village": 10}[level],
        "algorithm": 1,
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
    keys = ("level", "year", "start_month", "end_month", "scale", "algorithm", *LEVELS[:-1])
    identity = {key: config[key] for key in keys if key in config}
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def snapshot_page(scope_key, page=1, page_size=50, search="", sort_by="rank", sort_dir="asc"):
    """Read a completed ranking from PostgreSQL without contacting Earth Engine."""
    db: Session = SessionLocal()
    try:
        snapshot = db.get(GreenRankingSnapshot, scope_key)
        if snapshot is None:
            raise AnalysisError("Snapshot peringkat wilayah tidak ditemukan.", 404)
        page = max(1, int(page))
        page_size = max(10, min(100, int(page_size)))
        search = str(search or "").strip()[:100]
        sort_dir = "desc" if str(sort_dir).lower() == "desc" else "asc"
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
        }
        if sort_by not in sort_columns and sort_by != "rank":
            sort_by = "rank"
        query = db.query(GreenRankingSnapshotRow).filter_by(scope_key=scope_key)
        if search:
            term = f"%{search}%"
            query = query.filter(
                or_(GreenRankingSnapshotRow.name.ilike(term), GreenRankingSnapshotRow.code.ilike(term))
            )
        records_filtered = query.count()
        if sort_by == "rank":
            order = GreenRankingSnapshotRow.rank.asc().nullslast()
            if sort_dir == "desc":
                order = GreenRankingSnapshotRow.rank.desc().nullslast()
            query = query.order_by(GreenRankingSnapshotRow.eligible.desc(), order, GreenRankingSnapshotRow.code.asc())
        else:
            column = sort_columns[sort_by]
            order = column.desc().nullslast() if sort_dir == "desc" else column.asc().nullslast()
            query = query.order_by(order, GreenRankingSnapshotRow.code.asc())
        stored_rows = query.offset((page - 1) * page_size).limit(page_size).all()
        now = datetime.now(UTC)
        config = snapshot.config
        return {
            "run_id": f"snapshot:{scope_key}",
            "cursor": scope_key,
            "done": True,
            "config": config,
            "page": page,
            "page_size": page_size,
            "records_total": snapshot.row_count,
            "records_filtered": records_filtered,
            "search": search,
            "sort_by": sort_by,
            "sort_dir": sort_dir,
            "rows": [
                {**row.payload, "rank": row.rank}
                for row in stored_rows
            ],
            "statistics": snapshot.statistics,
            "status": "complete",
            "method": snapshot.method,
            "boundary_source": snapshot.boundary_source,
            "source_url": snapshot.source_url,
            "snapshot_saved_at": snapshot.refreshed_at.isoformat(),
            "snapshot_refresh_due": snapshot.refresh_due_at <= now,
        }
    finally:
        db.close()


def latest_snapshot(config, **page_options):
    return snapshot_page(snapshot_scope_key(config), **page_options)


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


def result_page(run, owner, page=1, page_size=50, search="", sort_by="rank", sort_dir="asc"):
    if run.startswith("snapshot:"):
        return snapshot_page(
            run.split(":", 1)[1],
            page=page,
            page_size=page_size,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
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
        }
        if sort_by not in sort_columns and sort_by != "rank":
            sort_by = "rank"
        direction = sort_dir.upper()
        order_clause = (
            f"eligible DESC, green {direction}, code {direction}"
            if sort_by == "rank"
            else f"CASE WHEN {sort_columns[sort_by]} IS NULL THEN 1 ELSE 0 END ASC, {sort_columns[sort_by]} {direction}, code ASC"
        )
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
        search_clause = ""
        query_params = [run]
        if search:
            search_clause = " AND (lower(COALESCE(name_sort,'')) LIKE ? OR lower(code) LIKE ?)"
            pattern = f"%{search.lower()}%"
            query_params.extend([pattern, pattern])
        records_filtered = records_total
        if search:
            records_filtered = int(
                db.execute(
                    "SELECT COUNT(*) FROM rows WHERE run=? AND (lower(COALESCE(json_extract(payload,'$.name'),'')) LIKE ? OR lower(code) LIKE ?)",
                    (run, pattern, pattern),
                ).fetchone()[0]
            )
        query_params.extend([page_size, (page - 1) * page_size])
        rows = []
        for item_row in db.execute(
            f"""WITH ranked AS (
                SELECT code, payload, eligible, green, area, valid_area, green_area,
                    json_extract(payload,'$.name') AS name_sort,
                    json_extract(payload,'$.coverage_percent') AS coverage_sort,
                    json_extract(payload,'$.classes_ha.trees') AS trees_sort,
                    json_extract(payload,'$.classes_ha.grass') AS grass_sort,
                    json_extract(payload,'$.classes_ha.crops') AS crops_sort,
                    json_extract(payload,'$.classes_ha.shrubs') AS shrubs_sort,
                    json_extract(payload,'$.classes_ha.flooded') AS flooded_sort,
                    CASE WHEN eligible=1 THEN ROW_NUMBER() OVER (PARTITION BY eligible ORDER BY green DESC, code ASC) END AS official_rank
                FROM rows WHERE run=?
            )
            SELECT payload, official_rank FROM ranked
            WHERE 1=1{search_clause}
            ORDER BY {order_clause}
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
            "method": "Dynamic World: mode kelas periode terpilih; hijau = pohon, rumput, vegetasi tergenang, tanaman, semak. Persentase memakai luas batas administrasi penuh. Wilayah dengan cakupan data <80% tidak diberi peringkat. Urutan pada hasil parsial belum merupakan peringkat nasional lengkap.",
            "boundary_source": "Penyedia batas administrasi SAVEGEO; kode hierarki dan geometri per wilayah dipertahankan.",
            "source_url": "https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1",
        }


def run_batch(data):
    owner = str(data.get("_request_owner") or "")
    if not owner:
        raise AnalysisError("Jalankan peringkat melalui pekerjaan analisis terautentikasi.", 403)
    settings = data.get("extension_settings") or {}
    run = str(settings.get("run_id") or "")
    if run.startswith("snapshot:"):
        return snapshot_page(run.split(":", 1)[1])
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
            batch_size = {"province": 1, "city": 4, "district": 10, "village": 20}[config["level"]]
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
                collection = (
                    ee.ImageCollection("GOOGLE/DYNAMICWORLD/V1")
                    .filterBounds(ee.FeatureCollection(features).geometry())
                    .filterDate(start, end)
                )
                if not collection.size().getInfo():
                    raise AnalysisError("Tidak ada Dynamic World untuk batch dan periode ini.", 404)
                labels = collection.select("label").mode()
                area = ee.Image.pixelArea().divide(10000).updateMask(labels.mask())
                bands = area.rename("valid")
                for name, classes in {
                    "green": [1, 2, 3, 4, 5],
                    "trees": [1],
                    "grass": [2],
                    "crops": [4],
                    "shrubs": [5],
                    "flooded": [3],
                }.items():
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
                for feature in reduced.get("features", []):
                    props = feature["properties"]
                    candidate, key = context[str(props["code"])]
                    area_ha = float(props.get("region_area") or 0)
                    valid = float(props.get("valid") or 0)
                    green = float(props.get("green") or 0)
                    coverage = min(100, valid / area_ha * 100) if area_ha else 0
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


def snapshot_csv_rows(scope_key):
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
        "error",
    ]
    meta = snapshot_page(scope_key, page_size=10)

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        yield "\ufeff" + buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        db: Session = SessionLocal()
        try:
            query = (
                db.query(GreenRankingSnapshotRow)
                .filter_by(scope_key=scope_key)
                .order_by(GreenRankingSnapshotRow.eligible.desc(), GreenRankingSnapshotRow.rank.asc().nullslast())
                .yield_per(1000)
            )
            for record in query:
                row = dict(record.payload)
                row.update(
                    status="complete",
                    year=meta["config"]["year"],
                    start_month=meta["config"]["start_month"],
                    end_month=meta["config"]["end_month"],
                    level=meta["config"]["level"],
                    rank=record.rank or "",
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


def csv_rows(run, owner):
    """Export every computed region, including excluded/no-data rows and run status."""
    if run.startswith("snapshot:"):
        return snapshot_csv_rows(run.split(":", 1)[1])
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
