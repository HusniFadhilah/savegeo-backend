# Field observations and annual detrital carbon estimates

## Administrative workflow

`/admin/carbon-calibration` retains existing workbook/UAV imports and adds a
multi-site dataset form and a pool-specific field workspace. Supported pools
are AGB, BGB, SOC, litter (serasah) and deadwood (kayu mati). Litter and serasah
are the same pool, not additive components.

Enter plot observations manually or download the CSV header, preview a UTF-8
file, correct errors and commit the entire file atomically. Limits: 5 MB and
2,000 records per import. Existing workbook plots are preserved; they are not
silently converted when dates, sampling support or units are incomplete.

Each record retains its original value, quantity, unit, carbon fraction,
location, date, area, method, source and SHA-256. Reimporting the same observation
is idempotent. A conflicting observation at the same plot/date/pool/depth is
rejected, rather than overwriting history. New survey dates add visits. A plot
keeps the same calibration/validation role across visits for each pool.

Litter input can retain composite wet mass, subsample wet/dry mass, number and
area of quadrats, and the diameter boundary. Density uses the sampled quadrat
area, not the parent plot area. Deadwood input can retain standing/downed
components, volume, decay class and decay-appropriate dry density with its
source. Biomass is the sum of volume × dry density within the inventoried plot.
Transect-only volume must first be expanded using its documented sampling
design; this form does not silently assume it represents a whole-plot census.
Use nonoverlapping definitions for litter (< diameter boundary) and deadwood
(≥ boundary). Carbon fractions remain explicit. These methods preserve their
raw inputs and do not replace field measurements with default coefficients.

The CSV supports `litter_sample_json` and `deadwood_sample_json` as well as
`soil_layers_json`. New optional fields do not change checksums of earlier
records when they are absent. The workspace provides counts of independent
plots, a pool filter and prediction-source suggestions across all pages.

## Units and soil support

Results normalize to Mg C/ha. For dry biomass in kg/plot:

`carbon density = biomass_kg * 10 / plot_area_m2 * carbon_fraction`

The carbon fraction must be supplied explicitly for biomass. AGB and BGB
allometry/root:shoot references retain their derivation. BGB from a ratio is not
an independent root measurement.

SOC can be a laboratory-reported stock at an explicit depth, or a set of
contiguous measured soil layers. For each layer:

`SOC = organic_carbon_g_kg * bulk_density_g_cm3 * thickness_cm * 0.1 * (1 - coarse_fragments_volume_pct / 100)`

Bulk density here is fine-earth mass per fine-earth volume, not a bulk-density
definition that already incorporates the gravel correction. Coarse fragments
must be a volume percentage. Layer gaps/overlaps and incomplete target-depth
coverage are rejected. Source:
[FAO SOC stock protocol, Annex 4](https://www.fao.org/fileadmin/user_upload/GSP/MRV/GSP_MRV_Protocol_A4_EN_002.pdf).
Comparisons are fixed-depth stock comparisons, not equivalent-soil-mass change
estimates. Distinguish mineral soil from the litter layer to avoid double counts.

## Prediction pairing and evaluation

Attach original numeric raster/model density separately, with a versioned source
ID, year, source resolution, sampling support and SOC depth. CSV can include
these fields at import; predictions can also be appended afterwards. A new model
version uses a new source ID. Existing predictions from the same source cannot
be silently replaced.

Default pairing requires the survey year and prediction year to match. A year
tolerance up to five years and acceptance of point-pixel samples require explicit
selection. SOC field and raster depth intervals must match exactly. All excluded
pairs are listed with reasons. Repeated visits are averaged per plot before
fitting, giving plots equal weight.

The report keeps baseline error, leave-one-plot-out calibration error and
separate validation error distinct. Validation plots never fit the model. Final
linear coefficients refit all calibration plots; they are not averaged fold
coefficients. Fewer than three calibration plots or a constant predictor yields
no calibration artifact. Models are experimental and scoped to their dataset,
habitat, source and depth. Administrative approval/activation remains explicit.
Data and audit events commit together. Assessment runs preserve source IDs and a
snapshot checksum and can be downloaded as JSON.
The stock transform `max(0, intercept + slope * prediction)` is used in both
cross-validation and separate validation and recorded in new artifacts. Older
reports are retained with their original transform. Scatter plots distinguish
separate validation from cross-validation; large plots are visually thinned
while statistics and JSON retain all evaluated plots.

New routes under `/api/admin/carbon-calibration`:

- `GET /field-template`, `GET /field-methods`.
- `GET/POST /datasets/{id}/observations`.
- `GET /datasets/{id}/field-summary`; observations can be filtered with `pool`.
- `POST /datasets/{id}/observations/csv?preview=true|false`.
- `POST /datasets/{id}/observations/{observation_id}/predictions`.
- `POST /datasets/{id}/field-assessments` and existing `GET /runs/{run_id}`.

## Annual litter and deadwood

The main carbon analysis now runs five pools. Litter/deadwood use frozen Random
Forest models trained on Indonesian tropical non-mangrove forest reference
labels, with three spatial folds and 0.5-degree validation blocks. Reference
labels combine NASA/ORNL 2010 living AGB, long-term WorldClim rainfall, SRTM
elevation and coefficients from
[UNFCCC AR-TOOL12 v3, pp17–18](https://cdm.unfccc.int/methodologies/ARmethodologies/tools/ar-am-tool-12-v3.0.pdf).
They are **derived proxy labels, not field measurements**. Water/non-forest areas,
mangroves and areas outside the tropical domain are not assigned fabricated
measurements. The annual model transfers the reference-year relation to the
selected year's Landsat covariates; it does not validate annual changes.

Training retains sample data, tree artifacts, checksums, spatial validation
metrics and an Earth Engine/sklearn parity test. Models with non-positive
spatial R² are not published. A transaction-level PostgreSQL advisory lock
prevents duplicate first-use training. Later analyses reuse the registered
frozen model. Keep `MODEL_DIR` on persistent storage with its corresponding DB.

AOI/year/model/source/scale-specific density statistics, histogram, valid area
and total stock use `carbon_annual_estimates`, the same persistent store as the
other annual pools. Reopening an already computed administrative AOI/year can
reuse its summary while issuing a fresh temporary Earth Engine map ID. This
does not mean every province/city/island has already been precomputed. Current
year keys advance with the observation cutoff.

Blue carbon, green rankings and vegetation productivity are separate analysis
types (`mode=bluecarbon`, `green_rankings`, `productivity`). Their parameter
panels register with the page's main Run Analysis button.

## Deployment

Run `alembic upgrade head` before exposing the field workspace. Migration 0045
adds the observation table and uniqueness constraint. Persist uploads, model
artifacts and the database together. No production deployment is performed by
the migration or this document.
