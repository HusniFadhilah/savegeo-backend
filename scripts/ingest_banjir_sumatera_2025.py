"""One-off ingestion for "Banjir Sumatera 2025" - BlackSky/BSG tasking
imagery captured 2025-11-29/30 across flood-affected sites in Aceh, Sumatera
Utara, and Sumatera Barat. Mirrors `scripts/ingest_ntt_earthquake.py`'s
structure/conventions exactly (same COG-via-vsizip approach, same
DisasterEvent-per-location decision, same pre-quake/pre-flood Sentinel-2
lookup) - see `docs/banjir-sumatera-2025-ingestion-prompt.md` for the full
rationale.

Source folder: `C:\\Users\\ACER\\Downloads\\Banjir Sumatera 2025`

Live-verified against every `_metadata.json` in the source (2026-08-22)
before writing this list - NOT guessed:
- 8 locations are usable (georeferenced=True, orthorectified=True).
- "Sumut BSG-104-...-non-ortho.zip" is EXCLUDED: its own metadata says
  georeferenced=False, orthorectified=False, cloudCoverPercent=85.9 - not
  usable as a map layer at all.
- The loose `0e2d4c62-...-Tiff.tif` (1.9GB, no zip, no metadata.json) is
  EXCLUDED: no acquisition date or location can be derived from the file
  itself - ingest manually once its date/location is confirmed with the
  data owner.

Licensing: BlackSky imagery here is commercial (see LicenseAgreement text
inside each zip, same "ING Level-2" tier documented in the NTT-earthquake
prompt). User confirmed live (2026-08-22) they hold the license to use it
on this platform - not re-verified per-file here.

`district` is intentionally left empty for every location: `location_name`
(from the tasking order's own place name) and `province` (cross-checked
against each scene's actual footprint centroid) are the only geographic
fields asserted with confidence here - kecamatan/kabupaten-level detail
was not independently verified and should not be guessed.

Run from savegeo/backend with its venv active:
    python scripts/ingest_banjir_sumatera_2025.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SOURCE_DIR = Path(r"C:\Users\ACER\Downloads\Banjir Sumatera 2025")
PRE_FLOOD_WINDOW_DAYS = 60  # how far back to look for a clean Sentinel-2 scene

# (zip filename OR extracted-folder marker, location label, slug, province,
# subfolder under SOURCE_DIR). is_extracted=True means the entry is already
# an unzipped folder (ortho.tif/metadata.json sitting directly on disk), not
# a .zip - only "Sumatera Barat 1-ortho" is like that in this source set.
LOCATIONS = [
    dict(sub="30-11-2025", zip_name="Aceh Tenggara BSG-104-20251129-084819-407954813-ortho.zip",
         label="Aceh Tenggara", slug="aceh_tenggara", province="Aceh"),
    dict(sub="30-11-2025", zip_name="Batuphat Barat-ortho.zip",
         label="Batuphat Barat", slug="batuphat_barat", province="Aceh"),
    dict(sub="30-11-2025", zip_name="Cot Kuta-ortho.zip",
         label="Cot Kuta", slug="cot_kuta", province="Aceh"),
    dict(sub="30-11-2025", zip_name="Sipirok BSG-113-20251129-072829-407836324-ortho.zip",
         label="Sipirok", slug="sipirok", province="Sumatera Utara"),
    dict(sub="30-11-2025", zip_name="Takengon BSG-104-20251129-084859-407957340-ortho.zip",
         label="Takengon", slug="takengon", province="Aceh"),
    dict(sub="30-11-2025", zip_name="Sumatera Barat 1-ortho", is_extracted=True,
         label="Sumatera Barat 1", slug="sumatera_barat_1", province="Sumatera Barat"),
    dict(sub="01-12-2025", zip_name="Aceh - BSG-105-20251130-080649-408120110-ortho.zip",
         label="Aceh (BSG-105)", slug="aceh_bsg105", province="Aceh"),
    dict(sub="01-12-2025", zip_name="Sumut - BSG-104-20251130-080129-408120097-ortho.zip",
         label="Sumatera Utara (BSG-104)", slug="sumut_bsg104", province="Sumatera Utara"),
]


def find_ortho_entry(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        candidates = [n for n in zf.namelist() if n.endswith("_ortho.tif")]
        if not candidates:
            raise FileNotFoundError(f"No *_ortho.tif entry in {zip_path.name}")
        return candidates[0]


def read_metadata_from_zip(zip_path: Path, ortho_entry: str) -> dict:
    meta_entry = ortho_entry.replace("_ortho.tif", "_metadata.json")
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read(meta_entry))


def bbox_of(coords: list[list[float]]) -> tuple[float, float, float, float]:
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons), min(lats), max(lons), max(lats)


def ingest_one(db, loc: dict, created_by: int | None) -> None:
    from app.repositories import disaster_repo
    from app.services import imagery_service, local_imagery_tile_service

    label, slug, province = loc["label"], loc["slug"], loc["province"]
    print(f"\n=== {label} ({slug}) ===")

    if loc.get("is_extracted"):
        folder = SOURCE_DIR / loc["sub"] / loc["zip_name"]
        meta_files = list(folder.glob("*_metadata.json"))
        ortho_files = list(folder.glob("*_ortho.tif"))
        if not meta_files or not ortho_files:
            print(f"  SKIP: expected _metadata.json + _ortho.tif in {folder}")
            return
        meta = json.loads(meta_files[0].read_text())
        src_raster_path = str(ortho_files[0])
    else:
        zip_path = SOURCE_DIR / loc["sub"] / loc["zip_name"]
        if not zip_path.exists():
            print(f"  SKIP: source not found: {zip_path}")
            return
        ortho_entry = find_ortho_entry(zip_path)
        meta = read_metadata_from_zip(zip_path, ortho_entry)
        src_raster_path = f"/vsizip/{zip_path.as_posix()}/{ortho_entry}"

    if not meta.get("georeferenced") or not meta.get("orthorectified"):
        print(f"  SKIP: not georeferenced/orthorectified ({meta.get('georeferenced')}/{meta.get('orthorectified')})")
        return

    geometry = meta["geometry"]
    coords = geometry["coordinates"][0]
    west, south, east, north = bbox_of(coords)
    acquired = dt.date.fromisoformat(meta["acquisitionDate"][:10])
    cloud_pct = float(meta.get("cloudCoverPercent") or 0.0)
    gsd = float(meta.get("gsd") or 0.0)
    sensor = meta.get("sensorName", "BlackSky")

    # --- 1. COG conversion (idempotent - skips if already done) ---
    from app.core.config import get_settings

    out_dir = get_settings().disaster_raster_path / "banjir_sumatera_2025"
    out_dir.mkdir(parents=True, exist_ok=True)
    cog_path = out_dir / f"{slug}_post_cog.tif"
    print(f"  COG: {cog_path.name} ({'exists' if cog_path.exists() else 'converting (can take a few minutes)...'})")
    local_imagery_tile_service.ensure_cog(src_raster_path, str(cog_path))

    # --- 2. DisasterEvent ---
    event = disaster_repo.create_event(
        db,
        {
            "name": f"Banjir Sumatera 2025 - {label}",
            "disaster_type": "flood",
            "location_name": label,
            "province": [province],
            "event_date": acquired,
            "status": "draft",
            "severity": "high",
            "description": (
                f"Banjir Sumatera akhir November 2025, lokasi {label} ({province}). "
                f"Citra tasking pasca-banjir: BlackSky/BSG (komersial - lisensi "
                f"dimiliki pengguna platform), sensor {sensor}, akuisisi "
                f"{acquired.isoformat()}, resolusi ~{gsd:.2f}m. "
                f"`event_date`/severity di sini adalah nilai awal berbasis tanggal "
                f"akuisisi citra - sesuaikan lewat Admin jika ada tanggal onset "
                f"atau tingkat keparahan resmi BNPB yang lebih presisi."
            ),
            "source": "BSG tasking + BNPB (citra komersial, lisensi pengguna platform)",
        },
        created_by,
    )
    print(f"  DisasterEvent id={event.id}")

    # --- 3. AOI from the BlackSky footprint ---
    aoi = disaster_repo.create_aoi(
        db,
        event.id,
        {
            "geojson": {"type": "Feature", "properties": {}, "geometry": geometry},
            "bbox": [west, south, east, north],
            "centroid": {"lat": (south + north) / 2, "lng": (west + east) / 2},
            "source": "upload_geojson",
        },
    )
    print(f"  AOI id={aoi.id}")

    # --- 4. "post" imagery: BlackSky, local COG ---
    post_img = disaster_repo.add_imagery(
        db,
        event.id,
        {
            "phase": "post",
            "satellite": "BlackSky",
            "acquisition_date": acquired,
            "sensor": sensor,
            "resolution_m": gsd,
            "cloud_coverage_pct": cloud_pct,
            "data_source": "BlackSky (komersial - lisensi dimiliki pengguna platform)",
            "is_primary": True,
            "source_kind": "local_upload",
            "local_file_path": str(cog_path),
        },
    )
    tile_template = f"/api/disasters/imagery-tiles/{post_img.id}/{{z}}/{{x}}/{{y}}.png"
    disaster_repo.set_preview_tile_url(db, post_img.id, tile_template)
    print(f"  post imagery id={post_img.id} -> {tile_template}")

    # --- 5. "pre" imagery: best Sentinel-2 scene before the flood ---
    aoi_payload = {"west": west, "south": south, "east": east, "north": north}
    start_date = (acquired - dt.timedelta(days=PRE_FLOOD_WINDOW_DAYS)).isoformat()
    end_date = acquired.isoformat()  # exclusive - strictly before this scene's own capture
    try:
        scenes_res = imagery_service.list_scenes(
            {"aoi": aoi_payload, "satellite": "sentinel2", "start_date": start_date, "end_date": end_date}
        )
        scenes = [s for s in scenes_res["scenes"] if s["cloud_cover_pct"] is not None]
        if not scenes:
            print("  pre imagery: NO Sentinel-2 scene found in the pre-flood window - skipped, register manually later.")
            return
        best = min(scenes, key=lambda s: s["cloud_cover_pct"])
        tile_res = imagery_service.get_scene_tile({"satellite": "sentinel2", "scene_id": best["id"], "aoi": aoi_payload})
        pre_acquired = dt.date.fromisoformat(best["acquired_at"][:10])
        pre_img = disaster_repo.add_imagery(
            db,
            event.id,
            {
                "phase": "pre",
                "satellite": "Sentinel-2",
                "acquisition_date": pre_acquired,
                "sensor": "MSI",
                "resolution_m": 10.0,
                "cloud_coverage_pct": best["cloud_cover_pct"],
                "data_source": "Sentinel-2 L2A (Google Earth Engine, otomatis - scene paling bersih awan sebelum banjir)",
                "is_primary": True,
                "source_kind": "gee",
                "preview_tile_url": tile_res["tile_url"],
            },
        )
        print(f"  pre imagery id={pre_img.id} (Sentinel-2 {pre_acquired.isoformat()}, awan {best['cloud_cover_pct']}%)")
    except Exception as e:  # noqa: BLE001
        print(f"  pre imagery: FAILED ({e}) - skipped, register manually later.")


def main() -> None:
    from app.db.session import SessionLocal
    from app.services.gee_service import initialize_ee

    with SessionLocal() as db:
        if not initialize_ee(db):
            print("WARNING: Earth Engine did not initialize - pre-flood Sentinel-2 lookup will fail for every location.")
        for loc in LOCATIONS:
            try:
                ingest_one(db, loc, created_by=None)
            except Exception as e:  # noqa: BLE001
                print(f"  FAILED ({loc['label']}): {e}")


if __name__ == "__main__":
    main()
