"""Sync a CAMS archive with --scope event|world and completed UTC date bounds."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely import make_valid
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep

from app.services import cams_archive_service as archive
from app.services.cams_emissions_service import (
    FluxCell,
    GFAS_DATASET_ID,
    IFS_DATASET_ID,
    CamsRequestError,
    clipped_cell_area_m2,
    create_ads_archive_adapter,
    geodesic_cell_area_m2,
)

WORLD_BBOX = [-180.0, -90.0, 180.0, 90.0]


def geometry(payload: dict):
    if payload.get("type") == "FeatureCollection":
        return unary_union([geometry(f) for f in payload["features"]])
    result = shape(payload["geometry"] if payload.get("type") == "Feature" else payload)
    if not result.is_valid:
        result = make_valid(result)
    if result.geom_type == "GeometryCollection":
        result = unary_union([part for part in result.geoms if part.geom_type in {"Polygon", "MultiPolygon"}])
    if result.is_empty or result.geom_type not in {"Polygon", "MultiPolygon"}:
        raise CamsRequestError(
            "Batas wilayah tidak memiliki geometri poligon yang valid", 502, "boundary_invalid"
        )
    return result


def cell_areas(latitudes: np.ndarray, longitudes: np.ndarray, boundary) -> np.ndarray:
    dy, dx = abs(float(latitudes[1] - latitudes[0])), abs(float(longitudes[1] - longitudes[0]))
    result = np.zeros((len(latitudes), len(longitudes)), dtype=float)
    prepared = prep(boundary)
    for row, lat in enumerate(latitudes):
        for col, lon in enumerate(longitudes):
            cell = FluxCell(lon - dx / 2, lat - dy / 2, lon + dx / 2, lat + dy / 2, None)
            polygon = box(cell.west, cell.south, cell.east, cell.north)
            if prepared.contains(polygon):
                result[row, col] = geodesic_cell_area_m2(cell.west, cell.south, cell.east, cell.north)
            elif prepared.intersects(polygon):
                result[row, col] = clipped_cell_area_m2(cell, mapping(boundary))
    return result


def global_cell_areas(latitudes: np.ndarray, longitudes: np.ndarray) -> np.ndarray:
    """Compute global grid-cell areas by latitude row without polygon clipping."""
    dx = abs(float(longitudes[1] - longitudes[0]))
    dy = abs(float(latitudes[1] - latitudes[0]))
    west = max(-180.0, float(longitudes[0]) - dx / 2)
    east = min(180.0, west + dx)
    row_areas = np.asarray([
        geodesic_cell_area_m2(
            west,
            max(-90.0, float(latitude) - dy / 2),
            east,
            min(90.0, float(latitude) + dy / 2),
        )
        for latitude in latitudes
    ])
    return np.broadcast_to(row_areas[:, None], (len(latitudes), len(longitudes))).copy()


def statistics(values: np.ndarray, areas: np.ndarray, layer_id: str) -> dict:
    valid = np.isfinite(values) & (areas > 0)
    result = {
        "valid_cell_count": int(valid.sum()),
        "missing_cell_count": int(((~np.isfinite(values)) & (areas > 0)).sum()),
        "unit": "kg" if layer_id == "burnt_carbon" else "1",
    }
    if layer_id == "burnt_carbon":
        result.update(
            {
                "integrated_mass_kg": float(np.sum(values[valid] * areas[valid]) * 86_400)
                if valid.any()
                else None,
                "duration_seconds": 86_400,
                "max_flux_kg_m2_s": float(values[valid].max()) if valid.any() else None,
            }
        )
    else:
        result.update(
            {
                "area_weighted_mean": float(np.average(values[valid], weights=areas[valid]))
                if valid.any()
                else None,
                "maximum": float(values[valid].max()) if valid.any() else None,
            }
        )
    return result


def timestamp(value) -> datetime:
    return datetime.fromisoformat(np.datetime_as_string(value, unit="s")).replace(tzinfo=UTC)


def ingest_file(
    path: Path, directory: Path, layer_id: str, regions: list[dict], bbox: list[float], request: dict
) -> list[dict]:
    import xarray as xr

    definition = archive.LAYER_DEFINITIONS[layer_id]
    required_units = {
        "burnt_carbon": {"kg m**-2 s**-1", "kg m-2 s-1"},
        "organic_aod": {"~", "1", "dimensionless"},
    }
    expected_id = 210092 if layer_id == "burnt_carbon" else 210210
    is_global = not regions and bbox == WORLD_BBOX
    boundary = box(*bbox) if is_global else unary_union([geometry(r["geometry"]) for r in regions])
    source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    frames = []
    with xr.open_dataset(path) as ds:
        variable = ds[definition["source_variable"]]
        if (
            variable.attrs.get("GRIB_paramId") != expected_id
            or variable.attrs.get("units") not in required_units[layer_id]
        ):
            raise CamsRequestError(
                "Identitas atau satuan variabel ADS tidak sesuai", 502, "source_units_mismatch"
            )
        if variable.attrs.get("GRIB_stepType") != ("avg" if layer_id == "burnt_carbon" else "instant"):
            raise CamsRequestError("Interval variabel ADS tidak sesuai", 502, "source_interval_mismatch")
        if bbox == WORLD_BBOX and float(ds.longitude.max()) > 180:
            ds = ds.assign_coords(longitude=((ds.longitude + 180) % 360) - 180).sortby("longitude")
        west, south, east, north = bbox
        ds = ds.sel(latitude=slice(north + 1, south - 1), longitude=slice(west - 1, east + 1))
        lat, lon = ds.latitude.values, ds.longitude.values
        expected_spacing = 0.1 if layer_id == "burnt_carbon" else 0.4
        if (
            len(lat) < 2
            or len(lon) < 2
            or not np.allclose(np.diff(lat), -expected_spacing, atol=0.00001)
            or not np.allclose(np.diff(lon), expected_spacing, atol=0.00001)
        ):
            raise CamsRequestError("Grid ADS tidak sesuai", 502, "source_grid_mismatch")
        transform = from_origin(
            float(lon[0]) - expected_spacing / 2,
            float(lat[0]) + expected_spacing / 2,
            expected_spacing,
            expected_spacing,
        )
        overall_areas = global_cell_areas(lat, lon) if is_global else cell_areas(lat, lon, boundary)
        region_areas = [(r, cell_areas(lat, lon, geometry(r["geometry"]))) for r in regions]
        variable = ds[definition["source_variable"]]
        dimensions = [d for d in variable.dims if d not in {"latitude", "longitude"}]
        for indices in np.ndindex(*(ds.sizes[d] for d in dimensions)):
            selection = dict(zip(dimensions, indices))
            field = ds.isel(selection)
            source_valid = timestamp(field.valid_time.values)
            start = source_valid - timedelta(days=1) if layer_id == "burnt_carbon" else source_valid
            reference = (
                timestamp(field.forecast_reference_time.values)
                if "forecast_reference_time" in field.coords
                else None
            )
            requested_start, requested_end = [date.fromisoformat(v) for v in request["date"].split("/")]
            request_date = reference.date() if reference else start.date()
            if not requested_start <= request_date <= requested_end:
                raise CamsRequestError(
                    "Tanggal berkas ADS tidak sesuai permintaan", 502, "source_time_mismatch"
                )
            if layer_id == "burnt_carbon" and (start.hour or start.minute or start.second):
                raise CamsRequestError(
                    "Rata-rata harian GFAS bukan interval 00–24 UTC", 502, "source_time_mismatch"
                )
            if reference:
                lead = (source_valid - reference).total_seconds() / 3600
                if (
                    lead not in [int(v) for v in request["leadtime_hour"]]
                    or reference.strftime("%H:%M") not in request["time"]
                ):
                    raise CamsRequestError(
                        "Waktu prakiraan ADS tidak sesuai permintaan", 502, "source_time_mismatch"
                    )
            values = (
                field[definition["source_variable"]]
                .transpose("latitude", "longitude")
                .values.astype("float32")
            )
            missing = variable.attrs.get("GRIB_missingValue")
            if missing is not None:
                values = np.where(values == np.float32(missing), np.nan, values).astype("float32")
            if np.any(values[np.isfinite(values)] < 0):
                raise CamsRequestError("Nilai ADS negatif tidak didukung", 502, "negative_source_values")
            if layer_id == "burnt_carbon":
                values = np.where(overall_areas > 0, values, np.nan).astype("float32")
            identifier = hashlib.sha256(f"{layer_id}:{start.isoformat()}:{reference}".encode()).hexdigest()[
                :24
            ]
            filename = identifier + ".tif"
            temporary = directory / (filename + ".tmp")
            with rasterio.open(
                temporary,
                "w",
                driver="GTiff",
                width=len(lon),
                height=len(lat),
                count=1,
                dtype="float32",
                crs="EPSG:4326",
                transform=transform,
                nodata=np.nan,
                tiled=True,
                blockxsize=128,
                blockysize=128,
                compress="deflate",
            ) as raster:
                raster.write(values, 1)
                raster.update_tags(
                    dataset_id=GFAS_DATASET_ID if layer_id == "burnt_carbon" else IFS_DATASET_ID,
                    source_unit=definition["source_unit"],
                    interval_start=start.isoformat(),
                    source_valid_at=source_valid.isoformat(),
                    source_sha256=source_sha,
                )
            os.replace(temporary, directory / filename)
            lead = int((source_valid - reference).total_seconds() / 3600) if reference else None
            summary = statistics(values, overall_areas, layer_id)
            maximum = float(np.nanmax(values)) if np.isfinite(values).any() else 0
            frames.append(
                {
                    "id": identifier,
                    "filename": filename,
                    "sha256": hashlib.sha256((directory / filename).read_bytes()).hexdigest(),
                    "version": "1.4.2" if layer_id == "burnt_carbon" else "current",
                    "valid_at": start.isoformat().replace("+00:00", "Z"),
                    "interval_end": source_valid.isoformat().replace("+00:00", "Z")
                    if layer_id == "burnt_carbon"
                    else None,
                    "forecast_reference_time": reference.isoformat() if reference else None,
                    "leadtime_hours": lead,
                    "is_forecast": bool(lead and lead > 0),
                    "summary": summary,
                    "regions": [
                        {"code": r["code"], "name": r["name"], "summary": statistics(values, areas, layer_id)}
                        for r, areas in region_areas
                    ],
                    "maximum_display_value": maximum * definition["multiplier"],
                    "provenance": {
                        "dataset_id": GFAS_DATASET_ID if layer_id == "burnt_carbon" else IFS_DATASET_ID,
                        "source_url": "https://ads.atmosphere.copernicus.eu/datasets/"
                        + (GFAS_DATASET_ID if layer_id == "burnt_carbon" else IFS_DATASET_ID),
                        "retrieved_at": datetime.now(UTC).isoformat(),
                        "source_sha256": source_sha,
                        "source_unit": definition["source_unit"],
                        "source_valid_at": source_valid.isoformat(),
                        "request": request,
                        "temporal_step": "rolling_24h" if layer_id == "burnt_carbon" else "instant",
                        "boundary_source": "Global WGS84 raster extent" if is_global else "SP3STAB; union of event provinces; invalid polygons repaired with make_valid",
                        "boundary_sha256": hashlib.sha256(
                            json.dumps(mapping(boundary), sort_keys=True).encode()
                        ).hexdigest(),
                        "fire_only": layer_id == "burnt_carbon",
                    },
                }
            )
    return frames


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", default="kalimantan-2026")
    parser.add_argument("--scope", choices=("event", "world"), default="event")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--forecast", action="store_true")
    args = parser.parse_args()
    if (
        args.start < date(2026, 1, 1)
        or not 0 <= (args.end - args.start).days < 31
        or args.end >= datetime.now(UTC).date()
    ):
        parser.error("Select 1–31 completed UTC days")
    is_world_scope = args.scope == "world"
    if is_world_scope:
        archive_slug = "world"
        codes, names, bbox = [], [], WORLD_BBOX
    else:
        from app.api.routes.wildfire_events import _admin_geometry
        from app.db.models.disaster_event import DisasterEvent
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            event = db.query(DisasterEvent).filter_by(slug=args.slug, disaster_type="forest_fire").one()
            codes, names, bbox = event.province_codes or [], event.province or [], event.bbox
        if not codes or not bbox:
            parser.error("Event needs province codes and bbox")
        archive_slug = args.slug

    directory = archive.event_directory(archive_slug)
    directory.mkdir(parents=True, exist_ok=True)
    boundary_directory = directory.parent / "boundaries"
    boundary_directory.mkdir(exist_ok=True)
    regions = []
    for index, code in enumerate(codes):
        path = boundary_directory / f"province-{code}.json"
        if path.exists():
            boundary = geometry(json.loads(path.read_text(encoding="utf-8")))
        else:
            boundary = geometry(_admin_geometry("province", code))
            path.write_text(json.dumps(mapping(boundary)), encoding="utf-8")
        regions.append(
            {
                "code": code,
                "name": names[index] if index < len(names) else code,
                "geometry": mapping(boundary),
            }
        )
    source_dir = directory.parent / "sources"
    source_dir.mkdir(exist_ok=True)
    adapter = None
    requests = []
    if not is_world_scope:
        requests.append((
            "burnt_carbon",
            GFAS_DATASET_ID,
            {
                "variable": ["wildfire_overall_flux_of_burnt_carbon"],
                "date": f"{args.start}/{args.end}",
                "time": ["00:00"],
                "temporal_aggregation": ["daily"],
                "data_format": "netcdf",
            },
        ))
    aod_request = {
        "variable": ["organic_matter_aerosol_optical_depth_550nm"],
        "date": f"{args.start}/{args.end}",
        "time": ["00:00", "12:00"],
        "leadtime_hour": ["0"],
        "type": ["forecast"],
        **({} if is_world_scope else {"area": [bbox[3] + 1, bbox[0] - 1, bbox[1] - 1, bbox[2] + 1]}),
        "data_format": "netcdf_zip",
    }
    requests.append(("organic_aod", IFS_DATASET_ID, aod_request))
    if args.forecast:
        requests.append(
            (
                "organic_aod",
                IFS_DATASET_ID,
                {
                    **aod_request,
                    "date": f"{args.end}/{args.end}",
                    "time": ["12:00"],
                    "leadtime_hour": [str(i) for i in range(0, 121, 3)],
                },
            )
        )
    try:
        manifest = archive.read_archive(archive_slug)
    except CamsRequestError as exc:
        if exc.code != "data_unavailable":
            raise
        manifest = {"schema_version": 1, "event_slug": archive_slug, "bbox": bbox, "layers": {}}
    for layer_id, dataset, request in requests:
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:24]
        source = source_dir / (digest + (".nc" if layer_id == "burnt_carbon" else ".zip"))
        if not source.exists():
            temporary = source.with_suffix(".part")
            adapter = adapter or create_ads_archive_adapter()
            adapter.retrieve(dataset, request, temporary)
            os.replace(temporary, source)
        inputs = [source]
        if source.suffix == ".zip":
            inputs = []
            with zipfile.ZipFile(source) as z:
                for name in z.namelist():
                    if name.endswith(".nc"):
                        target = source_dir / (digest + "-" + Path(name).name)
                        target.write_bytes(z.read(name))
                        inputs.append(target)
        definition = archive.LAYER_DEFINITIONS[layer_id]
        layer = manifest["layers"].setdefault(
            layer_id,
            {
                "product": definition["product"],
                "variable": definition["variable"],
                "source_unit": definition["source_unit"],
                "display_unit": definition["display_unit"],
                "palette": definition["palette"],
                "resolution_degrees": 0.1 if layer_id == "burnt_carbon" else 0.4,
                "frames": [],
            },
        )
        existing = {f["valid_at"]: f for f in layer["frames"]}
        for input_file in inputs:
            for frame in ingest_file(input_file, directory, layer_id, regions, bbox, request):
                previous = existing.get(frame["valid_at"])
                if previous is None or (frame["forecast_reference_time"] or "") >= (
                    previous["forecast_reference_time"] or ""
                ):
                    existing[frame["valid_at"]] = frame
        layer["frames"] = sorted(existing.values(), key=lambda f: f["valid_at"])
        layer["maximum"] = max(f["maximum_display_value"] for f in layer["frames"])
        if layer_id == "burnt_carbon":
            available_days = {f["valid_at"][:10] for f in layer["frames"]}
            layer["missing_requested_days"] = [
                (args.start + timedelta(days=i)).isoformat()
                for i in range((args.end - args.start).days + 1)
                if (args.start + timedelta(days=i)).isoformat() not in available_days
            ]
        manifest["updated_at"] = datetime.now(UTC).isoformat()
        temporary_manifest = directory / "manifest.json.tmp"
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False), encoding="utf-8"
        )
        os.replace(temporary_manifest, directory / "manifest.json")
        print(f"Archived {layer_id}: {len(layer['frames'])} validated frames", flush=True)


if __name__ == "__main__":
    try:
        main()
    except CamsRequestError as exc:
        print(f"CAMS synchronization failed: {exc.code}")
        raise SystemExit(1) from None
