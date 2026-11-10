"""Resumable administrative green-cover ranking, using the existing region adapter.

Every response distinguishes partial coverage from a completed national ranking.
SQLite holds public regional aggregates and owner-bound run cursors across restarts.
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
from datetime import UTC, datetime, date
from calendar import monthrange

import ee

from app.core.config import get_settings
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


def result_page(run, owner, page=1, page_size=50):
    with connection() as db:
        item = db.execute("SELECT * FROM runs WHERE id=? AND owner=?", (run, owner)).fetchone()
        if item is None:
            raise AnalysisError("Peringkat wilayah tidak ditemukan untuk akun ini.", 404)
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
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
        rows = [
            json.loads(row["payload"])
            for row in db.execute(
                "SELECT payload FROM rows WHERE run=? ORDER BY eligible DESC, green DESC, code LIMIT ? OFFSET ?",
                (run, page_size, (page - 1) * page_size),
            )
        ]
        for index, row in enumerate(rows):
            row["rank"] = (page - 1) * page_size + index + 1 if row.get("eligible") else None
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
    with connection() as db:
        if not run:
            level = settings.get("level", "province")
            if level not in LEVELS:
                raise AnalysisError("Tingkat peringkat tidak valid.", 422)
            year = int(data.get("year", datetime.now(UTC).year))
            start_month = int(data.get("start_month", 1))
            end_month = int(data.get("end_month", 12))
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
            }
            for parent in LEVELS[: LEVELS.index(level)]:
                if settings.get(parent):
                    code = str(settings[parent])
                    if len(code) > 40 or not all(char.isdigit() or char in ".-_" for char in code):
                        raise AnalysisError("Kode filter wilayah tidak valid.", 422)
                    config[parent] = code
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
                    key = hashlib.sha256(
                        json.dumps([config, candidate["code"], checksum], sort_keys=True).encode()
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
                    cached = db.execute("SELECT payload FROM cache WHERE key=?", (key,)).fetchone()
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
    return result_page(run, owner)


def csv_rows(run, owner):
    """Export every computed region, including excluded/no-data rows and run status."""
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
