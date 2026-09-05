# SamGeo scene segmentation

The imagery viewer runs automatic SAM ViT-B segmentation on the selected STAC
COG asset, cropped and masked to the AOI. Results are geographic GeoJSON polygons,
not semantic land-cover classes. Band indices and rescale values follow the COG
render controls. Without explicit rescale, non-byte data uses a 2-98 percentile
stretch. Processing is limited to 1024 pixels on the longest side.

Install in a dedicated Python environment to keep the existing inference models'
NumPy/scikit-learn versions unchanged:

```powershell
python -m venv .venv-samgeo
.\.venv-samgeo\Scripts\python.exe -m pip install "segment-geospatial>=1.4.2,<2"
```

The worker uses `SAMGEO_PYTHON` when configured, otherwise the backend interpreter
if SamGeo is installed, then `.venv-samgeo`, then the existing development
`.venv-py312-test` environment. On Linux the environment uses `bin/python`.
Alternatively install the backend's `samgeo` extra in a compatible environment.

The first run downloads the official ViT-B checkpoint into
`backend/var/samgeo/checkpoints`. CPU inference may take several minutes. A job
has a 15-minute inference timeout; only one inference runs at a time across API
workers sharing the same filesystem. Persist `var/samgeo` when deploying;
separate hosts need a shared job backend before running this service as a cluster.
After a process crash, the stale lock can be reclaimed after 30 minutes. GeoJSON
results remain in each job's `status.json`; temporary rasters are removed when a
job finishes. Administrators can remove old completed job directories as needed.

Endpoints:

- `GET /api/imagery/samgeo/status`: dependency availability.
- `POST /api/imagery/samgeo/jobs`: `{item_url, asset_key, aoi, bands?, rescale?}`.
- `GET /api/imagery/samgeo/jobs/{uuid}`: running, failed, or complete with GeoJSON.

Verification from the backend directory:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_imagery_stac.py
.\.venv\Scripts\python.exe scripts/verify_imagery_stac_samgeo.py --samgeo
```

The live check exercises each frontend preset, TIFF download, nonblank PNG
rendering, and real SAM inference on NAIP imagery in San Francisco. It records
results in `var/imagery-verification.json`. NAIP covers the United States; a local
AOI outside that coverage correctly returns no scenes.

Upstream API: https://samgeo.gishub.org/samgeo/
