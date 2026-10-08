# Land cover and change integration

## Available sources

### Curated operational choices

The default land-cover picker and agent dataset discovery offer five distinct
published sources: MapBiomas Indonesia 4.1 (detailed Indonesian annual LULC),
Dynamic World (recent month/date monitoring), Esri annual (global 10 m annual
maps), GLC_FCS30D (long historical series and detailed classes), and ESA
WorldCover (single-year reference). The change picker excludes WorldCover:
2020 and 2021 use different algorithm versions. This is a selection by use
case and operational readiness, not a claim that one product is most accurate
for every AOI or class.

GeoSave Dynamic World, GeoSave MapBiomas, and Esri Living Atlas are aliases;
the pickers use their canonical keys and deduplicate multi-source requests.
Saved alias URLs remain supported. Legacy JAXA FNF and FROM-GLC10 are omitted
from new selections. Coarse/reference products, forest/mangrove specialists,
and trained model adapters require the explicit advanced option. Reference RF
models have not undergone independent local validation. GLAD is unavailable
until its raster URI and actual class schema have been verified.

`GET /api/landcover/datasets` retains the full compatibility catalog with
`recommended` and `selection_tier`. `GET /api/datasets?module=landcover` returns
the primary sources; `include_advanced=true` adds canonical advanced sources
and `include_unavailable=true` also exposes unavailable entries. Aliases and
deprecated entries are not independent discovery choices.

| Source | Native pixels | Years | Schema / use |
| --- | --- | --- | --- |
| MapBiomas Indonesia 4.1 | 30 m | 1990–2024 | 13 detailed Indonesian classes; annual monitoring |
| GLC_FCS30D | 30 m | 1985, 1990, 1995; 2000–2022 annual | 35 fine classes; long historical monitoring |
| Esri annual | 10 m | 2017–2025 | Eight thematic classes; cloud code and nodata excluded |
| AlphaEarth + RF | 10 m features | 2017–2025 | RF trained on homogeneous MapBiomas 4.1 reference labels |
| Sentinel-1/2 + RF | 10 m features | 2017–present | Same reference schema; annual/month/date composites |
| TerraMind / TESSERA / Prithvi | Configured | Actual published years | Trained predictions, with an explicit class schema and version |
| Copernicus LCFM10 | 10 m | Published years only | Eleven classes; reference 2020 is the verified release |

The foundation model entries are inactive until trained prediction assets are
configured. A pretrained backbone or generated TerraMind mental image is not
a validated land-cover map. Default RF training uses a bounded weak-label
sample from twelve Indonesian regions; the response reports missing training
classes and has no claimed independent accuracy. A 30 m training reference
does not establish 10 m thematic accuracy. Monthly RF classification has not
been independently validated for seasonal change.

The Living Atlas catalog alias uses the Esri EE annual source for both maps
and pixel analysis, and reports `analysis_source`; it does not mix an ArcGIS
visualization with a different raster for statistics.

## User workflows

1. Open land cover, select AOI, source(s), and an available year. The source
   cards show class count, version, coverage, years, and accuracy limitations.
2. Open land-cover change, select source and two or more available years.
3. Annual class areas, maps, class identification, actual pixel transition
   matrix, gross gain/loss, net change, and ranked hotspots share one loader.
4. Transitions use nearest-neighbor categorical sampling, one equal-area
   EPSG:6933 grid, and the common valid mask. Nodata/clouds and unknown codes
   do not become land-cover transitions. Matrix values are hectares.
5. Repeated effective rasters and incompatible schema/classifier versions
   are rejected. ESA WorldCover 2020 and 2021 use different classifier
   versions and therefore are not accepted as a consistent change pair.
6. The selected RF/Dynamic World confidence threshold applies to classification,
   transition, maps, identification, and hotspots. RF vote confidence is not
   calibrated accuracy. Hotspot polygon confidence is reported only where
   the workflow has a probability surface, including RF votes and configured
   prediction confidence bands; missing values remain null.

## Admin and durable classifiers

Open **Admin → Model Registry** (`/admin/ml-models`). The land-cover section
uses the existing `model.read` / `model.write` permissions. It provides:

- A catalog of model status and supported years.
- Validated JSON configuration (`GET/PUT /api/admin/landcover-models`).
- Training for AlphaEarth/Sentinel RF, exported as an EE classifier asset.
- Asynchronous local sampling status and EE export task status.
- Explicit activation after the classifier export completes.

Configure `LANDCOVER_MODEL_CONFIG_PATH` (default `var/landcover-models.json`)
in a **persistent directory shared by API workers**. Training status files
use `landcover-training/` in the same directory. The JSON file is not a secret
store and must not contain credentials. Credential administration remains
in the existing Earth Engine credential panel.

Saved classifiers avoid sampling again after API restarts. Reference sampling
tables are also cached atomically in the persistent training directory; RF
graphs are rebuilt from the same fixed samples after a restart. For production, train and
activate a saved classifier. Use a new asset/version for every training run;
do not overwrite a classifier in place. Activate the same classifier for
both comparison periods. EE export needs write permission on the destination.

Example saved classifier:

```json
{
  "AlphaEarth_RF": {
    "enabled": true,
    "classifier_asset": "projects/YOUR_PROJECT/assets/alphaearth_indonesia_rf_v1",
    "model_version": "alphaearth-mapbiomas41-rf-v1",
    "class_encoding": "indices",
    "validation_kind": "reference_labels",
    "accuracy_note": "MapBiomas 4.1 reference training; independent validation pending."
  }
}
```

When using a custom classifier, configure its actual `classes` mapping and
`class_encoding` (`indices`: dense sorted schema positions; `raw`: raw class
codes). Native scale is an integer, 10–1000 m. Comparison class IDs must be
unique integers 0–999, each with a unique label and a `#RRGGBB` color.

## Trained foundation model pipeline

Train a segmentation head/classifier using spatially separated training,
validation, and test labels. Report per-class precision/recall/F1, confusion
matrix, area-adjusted accuracy, source years, held-out geography, and change
validation. Do not report random pixel cross-validation as independent
geographical accuracy.

- **TESSERA**: obtain actual embedding rasters for the AOI/year, train a
  classifier on those embeddings, and save a trusted local joblib estimator
  that predicts raw schema codes.
- **TerraMind / Prithvi**: fine-tune a segmentation head using the correct
  modalities and normalization. Export a TorchScript wrapper taking a feature
  stack `[N,C,H,W]` and returning logits `[N,K,H,W]` at input resolution.
  Its wrapper must handle modality/time arrangement and metadata required by
  the trained model. The API does not download arbitrary checkpoints.
- **LCFM10**: ingest the official categorical raster unchanged. Its codes
  differ from ESA WorldCover: 10 trees, 20 shrubs, 30 grass, 40 crops, 50
  herbaceous wetland, 60 mangroves, 70 moss/lichen, 80 bare/sparse, 90 built-up,
  100 water, 110 snow/ice; 254 is unclassified and excluded.

`scripts/landcover_predict.py` runs trusted trained artifacts over a georeferenced
feature raster with bounded window memory. It preserves CRS/transform/mask,
uses halo cropping for segmentation, writes a two-band COG and a provenance
JSON with model/schema hashes. It never silently substitutes another model.
Install PyTorch on the **inference worker** only for TorchScript; joblib,
NumPy, rasterio, and scikit-learn are already backend dependencies.

Example input schema (replace values with the actual trained preprocessing):

```json
{
  "model_version": "tessera-indonesia-v1",
  "accuracy_note": "Independent local evaluation pending.",
  "class_codes": [3, 21, 24],
  "bands": [1, 2, 3], "scale": 1,
  "mean": [0, 0, 0], "std": [1, 1, 1]
}
```

```powershell
python scripts/landcover_predict.py --input features_2024.tif --model classifier.joblib --schema schema.json --backend joblib --year 2024 --output prediction_2024.tif
```

Publish the two output bands to EE as `landcover` and `confidence`, preserving
the categorical values and mask. Use mode/sample pyramiding for landcover and
mean for confidence. Stage on Cloud Storage with your existing credentials;
use the official EE raster ingestion manifest rather than treating band 2 as
a second class layer. Published tiles must be mosaicked to an annual image
asset before configuring the adapter. Use a new version namespace and keep
older assets immutable for reproducible comparisons.

```json
{
  "TESSERA_LULC": {
    "annual_asset_template": "projects/YOUR_PROJECT/assets/tessera_v1_{year}",
    "model_version": "tessera-indonesia-v1", "supported_years": [2024, 2025],
    "class_band": "landcover", "confidence_band": "confidence", "native_scale": 10,
    "validation_kind": "not_provided", "accuracy_note": "Independent evaluation pending.",
    "classes": {"3": {"label": "Forest", "color": "#006400"}, "21": {"label": "Agriculture", "color": "#ffff00"}, "24": {"label": "Urban", "color": "#ff0000"}}
  }
}
```

Only declare years with real published outputs. With a single release, LCFM
can supply a baseline map; a change pair needs two compatible releases.

## Sources

- [MapBiomas Indonesia 4.1 ATBD](https://landy.mapbiomas.id/assets/files/ATBD%20Mapbiomas%20ID%20Col%204.1%20EN.pdf)
- [Official MapBiomas 4.1 legend](https://github.com/mapbiomas/user-toolkit/blob/master/legend-colors/coverage/indonesia-collection-4.1.csv)
- [GLC_FCS30D paper and validation](https://essd.copernicus.org/articles/16/1353/2024/)
- [AlphaEarth annual embeddings](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_SATELLITE_EMBEDDING_V1_ANNUAL)
- [Google confirmation of embeddings 2025](https://developers.google.com/maps/innovators/newsletters/03-2026)
- [EE classifier training and persistence](https://developers.google.com/earth-engine/guides/classification)
- [TESSERA](https://github.com/ucam-eo/tessera)
- [TerraMind model card](https://huggingface.co/ibm-esa-geospatial/TerraMind-1.0-base)
- [Prithvi model card](https://huggingface.co/ibm-nasa-geospatial/Prithvi-EO-2.0-600M-TL)
- [LCFM product and class documentation](https://land.copernicus.eu/en/technical-library/product-user-manual-global-land-cover-10-m/@@download/file)
