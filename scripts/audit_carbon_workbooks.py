"""Read-only audit for the supplied multi-location workbooks and KHDTK DEM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.carbon_workbook_service import parse_workbook, validate_raster_coverage


def _audit_workbook(path: Path, dataset_id: str, year: int) -> dict:
    parsed = parse_workbook(path, dataset_id, year)
    return {
        "source": parsed["source"],
        "audit": parsed["audit"],
        "sheets": parsed["sheets"],
        "sample_records": parsed["records"][:3],
        "records_for_spatial_check": parsed["records"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revised", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--dem", type=Path, required=True)
    args = parser.parse_args()

    revised = _audit_workbook(args.revised, "bu_dessy_carbon_survey_revised", 2026)
    previous = _audit_workbook(args.previous, "bu_dessy_carbon_survey_previous", 2024)
    try:
        import rasterio

        with rasterio.open(args.dem) as raster:
            dem = {
                "driver": raster.driver,
                "width": raster.width,
                "height": raster.height,
                "count": raster.count,
                "dtype": raster.dtypes[0],
                "crs": raster.crs.to_string() if raster.crs else None,
                "bounds": list(raster.bounds),
                "resolution": list(raster.res),
                "nodata": raster.nodata,
                "overviews": raster.overviews(1),
                "tags": raster.tags(),
            }
    except ImportError as exc:
        raise SystemExit(f"rasterio is required for DEM auditing: {exc}") from exc

    revised_coverage = validate_raster_coverage(
        [record for record in revised["records_for_spatial_check"] if "khdtk" in record["site_id"].lower()],
        tuple(dem["bounds"]),
        dem["crs"] or "unknown",
    )
    previous_coverage = validate_raster_coverage(
        [record for record in previous["records_for_spatial_check"] if "khdtk" in record["site_id"].lower()],
        tuple(dem["bounds"]),
        dem["crs"] or "unknown",
    )
    revised.pop("records_for_spatial_check")
    previous.pop("records_for_spatial_check")
    print(
        json.dumps(
            {
                "revised": revised,
                "previous": previous,
                "dem": dem,
                "dem_coverage": {
                    "revised": revised_coverage,
                    "previous": previous_coverage,
                    "blocking": revised_coverage.get("blocking", True)
                    or previous_coverage.get("blocking", True),
                },
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
