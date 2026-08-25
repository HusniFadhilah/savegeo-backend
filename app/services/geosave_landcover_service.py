"""Optional GeoSave Engine adapters for land-cover raster sources."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from importlib.util import find_spec
from typing import Any


@dataclass(frozen=True)
class GeoSaveRasterProbe:
    available: bool
    loader: str
    uri: str
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_geotiff(uri: str) -> GeoSaveRasterProbe:
    """Return a lightweight GeoSave Engine load capability probe.

    GeoSave Engine's `GeoTile.from_geotiff()` is the intended local/COG loader.
    For remote GCS URIs we avoid forcing a pixel read during request setup;
    Earth Engine still performs the existing server-side `loadGeoTIFF` work for
    summary/tile generation, while this probe records whether the GeoSave loader
    is available in the active Python environment.
    """
    if find_spec("geosave_engine") is None:
        return GeoSaveRasterProbe(
            available=False,
            loader="geosave_engine.geodata.tile.GeoTile.from_geotiff",
            uri=uri,
            note="geosave_engine belum terpasang di environment Python aktif.",
        )

    from geosave_engine.geodata.tile.geotile import GeoTile

    return GeoSaveRasterProbe(
        available=True,
        loader=f"{GeoTile.__module__}.{GeoTile.__name__}.from_geotiff",
        uri=uri,
        note="GeoSave Engine tersedia; URI dapat dipakai sebagai sumber GeoTIFF/COG-compatible.",
    )
