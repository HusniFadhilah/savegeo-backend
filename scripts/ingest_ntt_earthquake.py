"""One-off ingestion for the NTT (Flores) M7.7 earthquake, 2026-08-15 (BMKG:
https://www.bmkg.go.id/gempabumi/berpotensi-tsunami/20260815050954 -
epicenter 30km NE of Nagekeo, tsunami alert, 53 dead/137 injured as of
2026-08-18, 5,089 aftershocks recorded by 2026-08-22).

For each of the 9 BlackSky-imaged locations in
`C:\\Users\\ACER\\Downloads\\NTT Earthquake`:
  1. Convert the source `_ortho.tif` (extracted from its zip via GDAL's
     /vsizip/, no full unzip needed) to a Cloud-Optimized GeoTIFF under
     settings.disaster_raster_path, via the already-existing
     `local_imagery_tile_service.ensure_cog()`.
  2. Create a DisasterEvent (one per location - `get_active_aoi()` only ever
     returns the single most-recent AOI per event, so one event can't cleanly
     hold 9 independent sites - see the integration prompt's Tugas 0.4).
  3. Create its AOI from the BlackSky capture's own footprint polygon
     (`_metadata.json` "geometry" - already real georeferenced coordinates,
     not hand-drawn).
  4. Register the BlackSky capture as "post" imagery (source_kind=
     "local_upload", tile-served by the new /disasters/imagery-tiles route).
  5. Look for a real Sentinel-2 scene from BEFORE 2026-08-15 at the same
     location/AOI (reusing app.services.imagery_service, built earlier this
     session) and register the least-cloudy one as "pre" imagery
     (source_kind="gee") - so every event gets a genuine, GEE-backed
     before/after pair, not just a lone post-quake image.

Run from savegeo/backend with its venv active:
    python scripts/ingest_ntt_earthquake.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SOURCE_DIR = Path(r"C:\Users\ACER\Downloads\NTT Earthquake")
EARTHQUAKE_DATE = dt.date(2026, 8, 15)
PRE_QUAKE_WINDOW_DAYS = 75  # how far back to look for a clean Sentinel-2 scene

# (zip filename, location label, slug) - slug is used for both the COG
# filename and the DisasterEvent's location_name/name.
LOCATIONS = [
    ("Labuan Bajo GeoTIFF.zip", "Labuan Bajo", "labuan_bajo"),
    ("Pelabuhan Maurole GeoTIFF.zip", "Pelabuhan Maurole", "pelabuhan_maurole"),
    ("Lambaleda 2 GeoTIFF.zip", "Lambaleda", "lambaleda_2"),
    ("Desa Riung Cibal 2 GeoTIFF.zip", "Desa Riung Cibal", "desa_riung_cibal_2"),
    ("Satar Punda Bar GeoTIFF.zip", "Satar Punda Bar", "satar_punda_bar"),
    ("Satar Punda Bar 2 GeoTIFF.zip", "Satar Punda Bar (2)", "satar_punda_bar_2"),
    ("Tagol 2 GeoTIFF.zip", "Tagol", "tagol_2"),
    ("Pelabuhan Laurentius Say GeoTIFF.zip", "Pelabuhan Laurentius Say", "pelabuhan_laurentius_say"),
    ("Desa Liang Deruk GeoTIFF", "Desa Liang Deruk", "desa_liang_deruk"),
]


def find_ortho_entry(zip_path: Path) -> str:
    with zipfile.ZipFile(zip_path) as zf:
        candidates = [n for n in zf.namelist() if n.endswith("_ortho.tif")]
        if not candidates:
            raise FileNotFoundError(f"No *_ortho.tif entry in {zip_path.name}")
        return candidates[0]


def read_metadata(zip_path: Path, ortho_entry: str) -> dict:
    meta_entry = ortho_entry.replace("_ortho.tif", "_metadata.json")
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read(meta_entry))


def bbox_of(coords: list[list[float]]) -> tuple[float, float, float, float]:
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return min(lons), min(lats), max(lons), max(lats)


def ingest_one(db, zip_name: str, label: str, slug: str, created_by: int | None) -> None:
    from app.repositories import disaster_repo
    from app.services import imagery_service, local_imagery_tile_service

    print(f"\n=== {label} ({slug}) ===")
    zip_path = SOURCE_DIR / zip_name
    if not zip_path.exists():
        print(f"  SKIP: source not found: {zip_path}")
        return

    ortho_entry = find_ortho_entry(zip_path)
    meta = read_metadata(zip_path, ortho_entry)
    geometry = meta["geometry"]  # GeoJSON Polygon, already real footprint
    coords = geometry["coordinates"][0]
    west, south, east, north = bbox_of(coords)
    acquired = dt.date.fromisoformat(meta["acquisitionDate"][:10])
    cloud_pct = float(meta.get("cloudCoverPercent") or 0.0)

    # --- 1. COG conversion (idempotent - skips if already done) ---
    from app.core.config import get_settings

    out_dir = get_settings().disaster_raster_path / "ntt_earthquake"
    out_dir.mkdir(parents=True, exist_ok=True)
    cog_path = out_dir / f"{slug}_post_cog.tif"
    vsi_path = f"/vsizip/{zip_path.as_posix()}/{ortho_entry}"
    print(f"  COG: {cog_path.name} ({'exists' if cog_path.exists() else 'converting...'})")
    local_imagery_tile_service.ensure_cog(vsi_path, str(cog_path))

    # --- 2. DisasterEvent ---
    event = disaster_repo.create_event(
        db,
        {
            "name": f"Gempa Bumi NTT (Flores) 2026 - {label}",
            "disaster_type": "earthquake",
            "location_name": label,
            "province": ["Nusa Tenggara Timur"],
            "event_date": EARTHQUAKE_DATE,
            "status": "draft",
            "severity": "critical",
            "description": (
                "Gempa bumi M7.7 Flores, NTT, 15 Agustus 2026 pukul 04:58 WIB "
                "(kedalaman 15km, episenter 30km timur laut Nagekeo, Flores Back-Arc "
                "Thrust). Berpotensi tsunami - terdeteksi di 16 lokasi/4 provinsi, "
                "tertinggi 1.605m di Pota, Manggarai Timur. 53 tewas, 137 luka-luka "
                "(per 18 Agustus 2026); 5.089 gempa susulan tercatat per 22 Agustus "
                "2026 (terbesar M6.2). Citra pasca-gempa: BlackSky (komersial, "
                f"akuisisi {acquired.isoformat()}, resolusi ~1.2m)."
            ),
            "source": "BMKG (https://www.bmkg.go.id/gempabumi/berpotensi-tsunami/20260815050954)",
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
            "sensor": "Global-2",
            "resolution_m": 1.2,
            "cloud_coverage_pct": cloud_pct,
            "data_source": "BlackSky (komersial - lisensi dimiliki pengguna platform)",
            "is_primary": True,
            "source_kind": "local_upload",
            "local_file_path": str(cog_path.resolve()),
        },
    )
    tile_template = f"/api/disasters/imagery-tiles/{post_img.id}/{{z}}/{{x}}/{{y}}.png"
    disaster_repo.set_preview_tile_url(db, post_img.id, tile_template)
    print(f"  post imagery id={post_img.id} -> {tile_template}")

    # --- 5. "pre" imagery: best Sentinel-2 scene before the earthquake ---
    aoi_payload = {"west": west, "south": south, "east": east, "north": north}
    start_date = (EARTHQUAKE_DATE - dt.timedelta(days=PRE_QUAKE_WINDOW_DAYS)).isoformat()
    end_date = EARTHQUAKE_DATE.isoformat()  # exclusive - strictly before the quake
    try:
        scenes_res = imagery_service.list_scenes(
            {"aoi": aoi_payload, "satellite": "sentinel2", "start_date": start_date, "end_date": end_date}
        )
        scenes = [s for s in scenes_res["scenes"] if s["cloud_cover_pct"] is not None]
        if not scenes:
            print("  pre imagery: NO Sentinel-2 scene found in the pre-quake window - skipped, register manually later.")
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
                "data_source": "Sentinel-2 L2A (Google Earth Engine, otomatis - scene paling bersih awan sebelum gempa)",
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
            print("WARNING: Earth Engine did not initialize - pre-quake Sentinel-2 lookup will fail for every location.")
        for zip_name, label, slug in LOCATIONS:
            try:
                ingest_one(db, zip_name, label, slug, created_by=None)
            except Exception as e:  # noqa: BLE001
                print(f"  FAILED ({label}): {e}")


if __name__ == "__main__":
    main()
