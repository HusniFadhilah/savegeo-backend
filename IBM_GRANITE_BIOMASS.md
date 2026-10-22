# IBM Granite geospatial biomass (optional carbon model)

SAVEGEO exposes IBM Granite only when the backend operator enables it and the
matching model runtime, configuration, and checkpoint are installed. The model
is kept separate from the Sentinel-2/STAC estimators because it expects NASA
HLS L30 reflectance bands and returns above-ground biomass (AGB).

## Server setup

1. Install the optional IBM/TerraTorch runtime in the same Python environment
   as the SAVEGEO backend. On a compatible Python 3.11 environment, the pinned
   upstream versions are available through
   `pip install -e '.[granite-biomass]'`. Select the appropriate PyTorch wheel
   for the server's CUDA setup; verify the runtime in an isolated environment
   before enabling production traffic.
2. Download the official `biomass_model.ckpt` from the IBM Granite model
   repository and place it in a server-owned, read-only model directory. Obtain
   the matching `configs/config.yaml` from IBM's official repository and keep it
   beside the checkpoint. Do not accept either path from a client request.
3. Calculate the checkpoint SHA-256 on the server (`sha256sum
   biomass_model.ckpt`) and set these backend environment variables:

   ```dotenv
   IBM_GRANITE_BIOMASS_ENABLED=true
   IBM_GRANITE_BIOMASS_CHECKPOINT_PATH=/opt/savegeo/models/granite/biomass_model.ckpt
   IBM_GRANITE_BIOMASS_CONFIG_PATH=/opt/savegeo/models/granite/config.yaml
   IBM_GRANITE_BIOMASS_CHECKPOINT_SHA256=<64-character-checkpoint-sha256>
   ```

   Restart the backend. The model appears in `/api/models?model_type=carbon`
   under `GEDI_L4A_MONTHLY` only after all files and optional modules are
   discoverable. If any setting is absent, SAVEGEO hides the model and the
   existing estimators continue to work.

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

IBM's repository states it is provided without a commitment to maintenance or
support. Keep the dependency environment and downloaded checkpoint pinned and
verified; do not silently upgrade TerraTorch or substitute a checkpoint.
