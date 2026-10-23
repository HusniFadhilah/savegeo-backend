# IBM Granite geospatial biomass (optional carbon model)

SAVEGEO exposes IBM Granite in the carbon model picker when TerraTorch is
installed. The production Docker image includes the optional Granite runtime.
The model checkpoint and config are downloaded from IBM's pinned Hugging Face
revision on the first analysis and stored under `MODEL_DIR/ibm_granite_geospatial_biomass`.
The production compose file mounts `/app/var`, so downloaded artifacts persist
across container restarts. The model remains separate from the
Sentinel-2/STAC estimators because it expects NASA HLS L30 reflectance bands and
returns above-ground biomass (AGB).

## Use it

1. Select **GEDI L4A Monthly** in the carbon reference dataset picker.
2. Choose **IBM Granite Geospatial Biomass · HLS** from the model picker.
3. Run the analysis. On first use, the backend downloads the official 350 MB
   checkpoint and config, verifies the checkpoint SHA-256, and caches both for
   subsequent runs. Earth Engine must be initialized to build the NASA HLS
   composite.

Local Python environments need the optional runtime installed once:

```bash
pip install -e '.[granite-biomass]'
```

The model is omitted from the picker when the optional runtime is unavailable.
To disable it on a server, set `IBM_GRANITE_BIOMASS_ENABLED=false`.

For operator-managed artifacts, configure all three values below. The
checkpoint path, config path, and SHA-256 must be supplied together; these
paths are server-side only.

```dotenv
IBM_GRANITE_BIOMASS_CHECKPOINT_PATH=/opt/savegeo/models/granite/biomass_model.ckpt
IBM_GRANITE_BIOMASS_CONFIG_PATH=/opt/savegeo/models/granite/config.yaml
IBM_GRANITE_BIOMASS_CHECKPOINT_SHA256=<64-character-checkpoint-sha256>
```

## Analysis behavior

- The estimator reads `NASA/HLS/HLSL30/v002`, uses B2–B7 as
  BLUE/GREEN/RED/NIR_NARROW/SWIR_1/SWIR_2, and multiplies HLS reflectance by
  10,000 to match IBM's published training means and standard deviations.
- HLS Fmask cloud, adjacency, shadow, snow/ice, and water pixels are excluded.
  The selected months are composited with a median and inference runs at 30 m.
- The result reports AGB × 0.47 as Mg C/ha. GEDI L4A is provenance for model
  training labels; SAVEGEO does not present it as AOI validation or as a
  reference raster queried for that run.
- It is an experimental global zero-shot estimate and has no local Indonesian
  field-validation metrics. Current integration returns AOI statistics and
  totals; it does not publish a prediction tile layer.
- Each request is limited to 250 km², 300,000 pixels, and a 30 MiB HLS raster
  download to bound Earth Engine transfer and server memory. Earth Engine must
  be initialized, TerraTorch inference is serialized per process, and a GPU is
  recommended for practical response time.

## Upstream references

- [IBM Granite model card](https://huggingface.co/ibm-granite/granite-geospatial-biomass)
- [IBM's archived code and configuration](https://github.com/ibm-granite/granite-geospatial-biomass)
- [NASA HLS L30 v2 Earth Engine catalog](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSL30_v002)

The automatic downloader pins repository revision
`b472652a3d223d51e888877a1365262687f6906d` and verifies the checkpoint against
SHA-256 `32e91101c75c02c94c7dfa86f48290457812b7a92b7adc903c74953b5fa28b65`.

IBM's repository states it is provided without a commitment to maintenance or
support. Keep the dependency environment and downloaded checkpoint pinned and
verified; do not silently upgrade TerraTorch or substitute a checkpoint.
