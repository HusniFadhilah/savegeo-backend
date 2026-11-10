# Carbon extensions

The main carbon stock action now estimates AGB, BGB, SOC, litter and deadwood.
See `CARBON_FIELD_CALIBRATION.md` for frozen detrital models, annual persistence
and field validation. Blue carbon, green rankings and productivity have separate
analysis types. Legacy coefficient scenario endpoints below remain available
for reproducibility; the main stock action does not use those manual scenarios.

## Methods and source semantics

- **Litter and deadwood:** field/literature carbon density (Mg C/ha), applied to
  an explicitly selected homogeneous AOI or annual Dynamic World tree mask.
  Optional tropical forest presets use IPCC 2019 Refinement, Volume 4 Chapter 2,
  Table 2.2, all vegetation types. Rainforest: litter 4.8 / deadwood 14.8;
  moist forest: 5.9 / 8.0; dry forest: 2.4 / 9.0 Mg C/ha. These are Tier 1
  scenarios, with substantial uncertainty, not calibrated annual predictions.
  The user must confirm the applicable forest climate; presets always use the
  tree mask. Server coefficients override client-supplied preset values.
- **Vegetation productivity:** annual MOD17A3HGF Collection 6.1 GPP/NPP.
  The factor 0.0001 kg C/m2 times 10 converts raw values to Mg C/ha/year.
  Valid negative NPP is retained. Only published years are accepted. NPP/GPP
  are not leaf-level measurements, net ecosystem exchange, or credit issuance.
  A single-image mosaic avoids the global MODIS sinusoidal footprint clipping
  error while retaining the selected year's values.
- **Blue carbon:** a habitat-specific AGB+BGB+sediment SOC scenario, with required
  carbon coefficients, source citation, and sediment depth. Habitat can be the
  Landsat mangrove baseline of 2000, or a user-mapped mangrove/seagrass/saltmarsh
  AOI explicitly identified as habitat. The 2000 mask is never described as
  present-day habitat. No burial rate, annual removal or avoided emissions are
  inferred from a stock coefficient.

Sources:

- https://www.ipcc-nggip.iges.or.jp/public/2019rf/pdf/4_Volume4/19R_V4_Ch02_Generic%20Methods.pdf
- https://developers.google.com/earth-engine/datasets/catalog/MODIS_061_MOD17A3HGF
- https://developers.google.com/earth-engine/datasets/catalog/LANDSAT_MANGROVE_FORESTS
- https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1

## Administrative green rankings

Province, city/regency, district and village rankings use SAVEGEO's existing
administrative dropdown/geometry adapter, rather than a second historical
boundary hierarchy. Parent filters are optional: the same traversal supports a
national run and any selected ancestor scope. The ranking uses the full boundary
of each administrative unit, not a clipped fragment of the map AOI.

Dynamic World's modal class for the selected months/year provides tree, grass,
flooded vegetation, crop and shrub areas. Green percentage uses the full boundary
area as denominator. Regions below 80% valid image coverage have no rank. The
response includes observed areas, individual vegetation classes, coverage,
unweighted mean/median/population standard deviation, area-weighted green share,
range and a distribution of regional percentages.

Runs are owner-bound and processed in batches of up to 20 regions, with up to
four parallel boundary requests. The traversal cursor and computed rows persist
in `UPLOAD_DIR/green-rankings/rankings.sqlite3`. Keep this directory on the
server's persistent volume. Local SQLite transactions and leases serialize a
run across workers on the same host. Deployments spanning multiple hosts need
shared job routing/storage or a migration of this cache to the central database.

Each initial response returns a durable run ID before computation. Subsequent
requests include its cursor so the analysis job cache cannot replay the previous
batch indefinitely. Closing/stopping the UI stops further scheduling; a current
batch can finish. Resume reads the last cursor from the authenticated server.
Partial results are explicitly labelled and are not a complete national ranking.
Failed geometries remain visible and are included in completeness statistics.
CSV export streams every computed row, not only the visible page.

## PDF raster maps

Each carbon pool, including additional module results, gets a separate raster
figure composed from that result's own tile URL, with AOI outline, orientation,
legend, units and provenance. Requests are bounded to four concurrent tiles.
A failed/missing raster aborts report generation with a user-facing error rather
than silently producing an incomplete report. Raster figures are previews, not
replacement georeferenced rasters.

## Large full-resolution GeoTIFFs

Set `GEE_EXPORT_BUCKET` to an existing Google Cloud Storage bucket writable by
the configured Earth Engine identity. Consumers also need read access to the
destination objects. This feature does not create buckets or change IAM.

The full-resolution export option starts an Earth Engine batch export at the
selected scale, using EPSG:6933 and COG output, split into 8192x8192-pixel files.
It does not coarsen the selected scale. Earth Engine quotas, storage capacity and
model computation limits still apply. All output parts must be downloaded from
the destination bucket when the task completes. No public sharing is enabled.

Task status uses an expiring signed token bound to the submitting account.
Direct downloads remain available separately, with explicit automatic-scale
adjustment for their smaller request limits. Selecting a finer scale than the
source/reference resolution does not add observational detail.

## API

- `GET /api/carbon/extensions/catalog`: Tier 1 preset catalog.
- `GET /api/carbon/extensions/productivity-years`: published MODIS years.
- `POST /api/analysis-jobs`, type `carbon`, extension `litter`, `deadwood`,
  `npp`, `gpp`, `bluecarbon` or `green_rankings`: existing authenticated queue.
- `GET /api/carbon/green-rankings/{run_id}?page=1`: owner-bound progress/statistics.
- `GET /api/carbon/green-rankings/{run_id}/export`: all computed rows as CSV.
- Existing GeoTIFF job, `export_mode=batch_gcs`: full-resolution batch export.
- `POST /api/download/raster-task-status`: owner-bound Earth Engine task status.
