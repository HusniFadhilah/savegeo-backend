# Kalimantan wildfire multi-source layers

The `feat/disaster-kalimantan-2026` feature adds authenticated, read-only
endpoints under `/api/disaster`. Requests are bounded to Kalimantan and 31
inclusive days. Independent source failures return individual statuses rather
than discarding successful layers. These layers are observations, not claims
about ignition cause or confirmed fire events.

## Setup and sources

- Set `NASA_FIRMS_MAP_KEY` only in the backend environment. Obtain a free key
  via [NASA FIRMS](https://firms.modaps.eosdis.nasa.gov/api/area/).
  NOAA-20/21/S-NPP VIIRS (375m) and Terra/Aqua MODIS (1km) are queried in
  windows of at most five days. Keys are never returned to the browser or
  included in user-visible errors. NASA transaction limits still apply;
  avoid repeated large-region loads. NRT historical availability is provider-dependent.
- [BMKG Geohotspot](https://datacuaca.bmkg.go.id/arcgis/rest/services/production/geohotspot/MapServer/0)
  is queried by acquisition date and bounding box, then clipped to exact AOI.
  Archive retention and external outages can produce empty/error results.
- [BIG](https://geoservices.big.go.id/gis/rest/services/DISIGT/BatasWilayah/MapServer/0)
  supplies selected province/kabupaten/kota geometry. The province AOI is the
  collection of its kabupaten/kota polygons. Region-option codes still come
  from the existing region API; the names are matched against BIG geometry.
- [CDSE STAC](https://documentation.dataspace.copernicus.eu/APIs/STAC.html)
  supplies up to 20 Sentinel-2 L2A scene footprints with real acquisition
  timestamps and cloud metadata. This adapter does **not** deliver RGB pixels
  or masquerade as a second dNBR algorithm. Pixel access requires CDSE
  authentication; the existing Sentinel-2 analysis remains Earth Engine-based.
- [MCD64A1](https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MCD64A1)
  and [VNP64A1](https://developers.google.com/earth-engine/datasets/catalog/NASA_VIIRS_002_VNP64A1)
  require initialized Earth Engine. Monthly 500m burn-date products are
  restricted to the requested days and valid QA pixels. Unpublished months
  return `no_data`; older months are never silently substituted. Areas are
  reported independently, never added to each other or to dNBR results.
- [BNPB InaRISK](https://gis.bnpb.go.id/server/rest/services/inarisk/INDEKS_BAHAYA_KARHUTLA/ImageServer)
  is a 100m **hazard index**, not actual burned area or a 2026 observation.
  WMS capability lookup uses `/server/services/...`, not `/rest/services/...`.
  It is off by default and is unavailable when its service fails.
- [SIPONGI](https://sipongi.menlhk.go.id/) is supported through explicit CSV/
  GeoJSON import, not an undocumented scraped API. CSV needs longitude/
  latitude or aliases lon/lng/bujur/lat/lintang; comma, semicolon and tab
  delimiters are supported. Aggregate counts without coordinates are rejected.
  Point/Polygon/MultiPolygon imports are capped at 2MB/5,000 features and
  labelled unverified. They remain session-local and retain original locations,
  including those outside the selected AOI. They are not included in thermal
  source counts or merged automatically with verified providers.

## Interpretation

Each thermal feature retains source, sensor, time, raw confidence, available
FRP, resolution, and original fields. Up to 2,000 points per source are
displayed; truncation is explicitly marked, so counts are not a census.
Cross-source fusion groups observations within 500m and 30min, preserving
all metadata. Same-source observations and missing-time observations are
never fused. The result is **not a count of distinct fires**. Province/city
counts follow visible thermal layers. SAM remains candidate segmentation
near thermal seeds; it does not prove active flame or identify ignition cause.

## Verification

```
.venv/Scripts/python.exe -m unittest tests.test_fire_multi_source -v
.venv/Scripts/python.exe scripts/verify_fire_sources.py
```

The latter performs bounded, read-only checks without keys or Earth Engine.
Provider outages must be reported honestly; unit tests do not prove live
availability. The endpoint requires the existing disaster-viewer permission.

Read-only verification on 2026-09-13: BIG returned 13 kabupaten/kota polygons
for Kalimantan Selatan. CDSE returned six scenes for the Banjarmasin-area
sample, including `2026-09-11T02:25:49.024Z`. BMKG returned HTTP 403 with a
Cloudflare challenge; WMS InaRISK timed out. No challenge bypass was attempted.
The local FIRMS key was not configured, and live FIRMS/EE monthly raster/SAM
inference were not verified by these checks. Automatic checks passed 21
backend/imagery regression cases and five frontend layer/control cases.
Visual browser verification was not performed because `agent-browser` was
not available in the environment. Source picker/state separation and stale
response guards follow the React skill review.
