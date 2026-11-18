# SAVEGEO Backend Operations Runbook

## Local workflow

### Security rollout requirements

The runtime image runs as UID/GID 10001. Before deploying it, back up the
database and runtime directory, verify the absolute runtime directory on the
server, and grant that UID read/write access to the mounted `var` directory.
Check existing model, upload and dMRV evidence files as well as newly created
files. The worker and API must mount the same runtime directory. Do not change
production ownership until the target directory and backup have been checked.
Keep existing host ownership and grant UID 10001 an explicit ACL on `var`, with
default ACLs on its directories. Test both the deployment account and UID 10001.
The production API binds to loopback behind Apache. `prepare_runtime_database.py`
verifies the Docker bridge gateway used by that proxy and records it as trusted;
Uvicorn proxy-header rewriting stays disabled so forwarding is interpreted once.

The web process loads carbon models from ONNX and JSON only. Keep legacy
pickle files and original metadata as backup. Convert trusted legacy models
offline with `python -m scripts.convert_trusted_carbon_model <file.pkl> --sha256
<independently-verified-hash>` and compare predictions before rollout. New
uploads accept ONNX, remain inactive and require review before activation.
Uploaded files must never be passed to the offline pickle converter.

`/api/live` checks process availability; `/api/ready` checks database access
and returns 503 on failure. Production smoke tests must also check schema
revision, authorized access and analysis results.

1. Create `.venv` with Python 3.11.
2. Install `pip install -e ".[dev]"`.
3. Copy `.env.example` to `.env` and set `DATABASE_URL`, JWT, GEE, and storage settings.
4. Run `alembic upgrade head`, `python -m scripts.seed_config`, and `python -m scripts.seed_rbac`.
5. Start `uvicorn app.main:app --reload --port 8086`.
6. Verify `/api/health` and `/docs`.

## Required checks

Run locally before opening a Pull Request:

```powershell
ruff check . --ignore E501 --ignore B904 --ignore B905
mypy app
bandit -r app -lll
pip-audit . --ignore-vuln PYSEC-2024-110
pytest
```

The CI workflow repeats these checks and adds Gitleaks and Trivy image scanning.

## GitHub Actions configuration

Production deployment secrets:

| Secret | Purpose |
|---|---|
| `DEPLOY_HOST` | Production server hostname/IP |
| `DEPLOY_SSH_PORT` | Production SSH port |
| `DEPLOY_USER` | Production SSH user |
| `BACKEND_REMOTE_DIR` | Backend path on production |
| `BACKEND_HEALTH_URL` | Backend health URL |
| `VPN_CONFIG_B64` | Base64 OpenVPN configuration |
| `VPN_AUTH_B64` | Base64 OpenVPN credentials |
| `SSH_KNOWN_HOSTS` | SSH host fingerprints |
| `SSH_PASSWORD` | Production SSH password |

Doltinuku deployment secrets:

| Secret | Purpose |
|---|---|
| `SFTP_DOLTINUKU_SERVER` | Doltinuku hostname/IP |
| `SFTP_DOLTINUKU_PORT` | SFTP/SSH port |
| `SFTP_DOLTINUKU_USERNAME` | SFTP/SSH user |
| `SFTP_DOLTINUKU_PASSWORD` | SFTP/SSH and sudo password |

AI review configuration uses `OCR_LLM_URL`, `OCR_LLM_AUTH_TOKEN`, `OCR_LLM_MODEL`, and optional `OCR_LLM_USE_ANTHROPIC`.

## Deployment sequence

1. Pull Request runs CI and AI code review.
2. Merge to `main` triggers the selected deployment workflow.
3. Lint, typecheck, security audit, tests, Gitleaks, and Trivy must pass.
4. Source is synchronized to the target server.
5. Database backup and Alembic migration run before restart.
6. Systemd/API health checks and public-domain checks run after restart.

The production and Doltinuku workflows are independent because they target different servers.

## Runtime layout

Production values are supplied by the server `.env`, not Git. Runtime data must persist outside source synchronization:

```text
var/uploads/
var/saved_models/
var/gee-credentials/
var/disaster_rasters/
var/exports/
var/carbon_tile_cache/
```

### Carbon COG cache and monitoring

Production compose mounts `./var/carbon_tile_cache` at
`/app/var/carbon_tile_cache` and sets `CARBON_TILE_CACHE_DIR` to that path.
Keep this directory on persistent storage so rendered COG tiles survive API
restarts and deployments. `CARBON_TILE_CACHE_TTL_SECONDS` controls expiry
(default 24 hours).

Probe external source reachability and tile counters with:

```bash
curl -fsS https://begeo.husnifd.my.id/api/carbon/datasets/health
curl -fsS https://begeo.husnifd.my.id/api/carbon/tiles/metrics
```

The response includes per-dataset HTTP status/latency and process-local tile
cache counters (`requests`, `cache_hits`, `cache_misses`, `source_reads`,
`rendered`, `empty`, and `errors`). COG tile render logs are emitted at INFO
with dataset, XYZ coordinates, source count, and elapsed milliseconds; source
read failures remain at DEBUG because missing ocean tiles are expected.
World-scale low-zoom requests that would open more than
`CARBON_TILE_MAX_SOURCE_TILES` COGs are skipped with a transparent response;
zooming into the AOI then requests the detailed, cacheable tiles.

For disaster imagery, database `local_file_path` values must point to the target server. Thumbnail URLs use `/disaster-thumbnails/...` and the web server must proxy that path to FastAPI.

### Local Ollama on the LEN H100 server

The chatbot supports Ollama through its OpenAI-compatible `/v1` API. When the
production API runs in Docker on the same LEN host as Ollama, configure the
server `.env` with the exact model name shown by `ollama list`:

```dotenv
AI_PROVIDER=ollama
AI_MODEL=<model-from-ollama-list>
OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
```

`docker-compose.prod.yml` maps `host.docker.internal` to the Linux host
gateway. Ollama binds to `127.0.0.1` by default, so allow the Docker bridge
interface by setting the service environment on the H100 host and restarting
Ollama:

```bash
sudo systemctl edit ollama
# under [Service]: Environment="OLLAMA_HOST=0.0.0.0:11434"
sudo systemctl daemon-reload
sudo systemctl restart ollama
curl --fail http://127.0.0.1:11434/api/tags
ollama ps

# From the deployed backend directory, verify the exact runtime path too:
python -m scripts.check_ollama
```

Keep port `11434` private to the host/VPN. Do not put Ollama credentials or
the VPN/SSH password in Git; the local OpenAI-compatible client uses the
required but ignored key value `ollama`. If the API and GPU server are
separate machines, set `OLLAMA_BASE_URL` to a private VPN address or an SSH
local-forward endpoint instead of exposing Ollama publicly. Select **Ollama
(lokal)** in Admin → System Config → AI Controller when the database contains
an existing `ai.provider` value, because database configuration takes
precedence over the environment fallback.

### Documented test accounts

Create or rotate the four role accounts only on the server, using temporary
environment variables. Do not put these values in `.env.example`, GitHub
Actions, or a commit:

```bash
export SAVEGEO_PASSWORD_SAVEGEOGEOSPATIAL='(value supplied by the operator)'
export SAVEGEO_PASSWORD_EXAMPLE_USER='(value supplied by the operator)'
export SAVEGEO_PASSWORD_DEMO_ADMIN='(value supplied by the operator)'
export SAVEGEO_PASSWORD_SUPER_ADMIN='(value supplied by the operator)'
.venv/bin/python -m scripts.seed_rbac
.venv/bin/python -m scripts.ensure_test_accounts
unset SAVEGEO_PASSWORD_SAVEGEOGEOSPATIAL SAVEGEO_PASSWORD_EXAMPLE_USER \
  SAVEGEO_PASSWORD_DEMO_ADMIN SAVEGEO_PASSWORD_SUPER_ADMIN
```

The command is idempotent. It assigns `SavegeoGeospatial` to
`geospatial_expert`, `example_user` to `viewer`, `demo_admin` to `admin`, and
`super_admin` to the full-access state (`role_id = NULL`). It prints usernames
and role names only; passwords and password hashes are never printed.

## Verification

```bash
curl -fsS https://begeo.husnifd.my.id/api/health
curl -fsSI https://savegeo.husnifd.my.id/
```

Check systemd and recent logs on the server:

```bash
systemctl is-active savegeo-backend.service
journalctl -u savegeo-backend.service -n 150 --no-pager
```

## Rollback

1. Stop the deployment workflow if it is still running.
2. Restore the previous application commit on the target server.
3. Restore the pre-deploy PostgreSQL dump if a migration must be reverted.
4. Restart `savegeo-backend.service`.
5. Verify `/api/health`, authentication, and the affected feature.

## dMRV foundation, evidence and review (migrations 0039–0044)

1. `alembic upgrade head` creates the project, membership, boundary, field-data, monitoring-period, durable-job, evidence-package, submission, and review-finding tables.
2. `python -m scripts.seed_rbac` grants the project-creation permission to the built-in `admin` role. Project reads and mutations still require explicit project membership and role checks.
3. The API stores ZIP packages under `DMRV_EVIDENCE_DIR` (`/app/var/dmrv/evidence` in production). This path is on the persistent `./var:/app/var` mount. Back up the database and this directory as a consistent set; package rows alone cannot recover the ZIP contents.
4. ZIP manifests and entry checksums are checked on verify and download. This is not object-lock/WORM storage and does not replace a retention policy or restore drill. No production restore drill has been completed for this feature.
5. Evidence packages built from the current field-summary snapshot are intentionally incomplete. Submission is blocked until every required component is provided and reviewed. `approved` means internal reviewer approval only; external verification and issuance require separately documented authorized records.
6. A rollback below migration `0043_dmrv_evidence_packages` removes submission/review tables; a rollback below `0042_dmrv_monitoring_periods` also removes evidence package metadata. Restore the database and evidence files from the same verified backup rather than downgrading production data in place.

## Reproducible model training (DVC and MLflow)

The optional `mlops` extra installs DVC and MLflow without adding them to the production API runtime. The committed `dvc.yaml` pipeline reads a field/reference CSV whose location and training parameters are set in `params.yaml`; sensitive measurements must be stored in a private DVC remote, not Git. Configure that remote in the operator's local DVC config before pushing data.

```bash
pip install -e '.[mlops]'
dvc remote add --local --default savegeo <private-object-storage-uri>
dvc add data/training/carbon.csv
dvc push
dvc repro train_grouped_carbon
```

The trainer rejects missing/non-finite values, fewer than five independent plot groups, constant targets, and models that fail the configured GroupKFold preflight when `--register` is used. Defaults require R2 >= 0 and candidate RMSE below the mean-baseline RMSE; adjust `training.minimum_r2` and `training.max_baseline_rmse_ratio` only for a documented domain policy. OOF metrics, baseline, dataset and artifact SHA-256 values, feature/target schema, seed, estimator settings, installed distribution versions and Git revision are written to the run manifest. `--mlflow` additionally logs parameters, metrics and artifacts to `MLFLOW_TRACKING_URI`; credentials and tracking URI belong in the deployment secret store. The project does not yet include a complete environment lockfile, so the resolved-version manifest improves auditability but does not by itself guarantee bit-for-bit reproduction across machines.

Without DVC, a candidate can be trained directly with `python -m scripts.train_reproducible_model` and the same CLI arguments. `--register` stores a content-addressed ONNX artifact as an inactive candidate; it never activates or makes the model default. Evaluation and independent review in Admin are still required before activation. DVC execution and MLflow connectivity require the operator's dataset, private remote and tracking service, so they are not claimed as production-verified here.

Reference-raster GEE training follows the same lifecycle: new saved ensembles receive content-derived version names, encrypted spatial out-of-fold evidence and the `evaluated` state. Training no longer switches the serving version. A reviewer still needs to inspect that evidence and activate the exact artifact in Admin.

Earth Engine analysis jobs (carbon, vegetation and land cover/change) pin the current serving-code identity. The pin includes the product version, deployment commit when available, a SHA-256 over the carbon, vegetation, land-cover, dataset-registry and shared GEE serving modules, and the configured land-cover model registry checksum. A queued job is rejected with 409 if the serving deployment or registry changes before execution. Database-backed ONNX and frozen GEE carbon ensembles additionally pin their row, version, artifact checksum and approval digest. The manifest records the serving revision for later report generation. This pins application code and registered model configuration; it does not freeze mutable collections/assets hosted by Earth Engine.

### Field predictions from GEE

`POST /api/admin/carbon-calibration/datasets/{dataset_id}/field-predictions` starts a stored `CarbonCalibrationRun` for one pool/year and a batch of up to 500 observations. It automatically selects the highest-scoring active GEE saved ensemble for that pool, requiring a current encrypted evaluation and independent approval. Pass `offset` to process later batches. The run endpoint is `GET /api/admin/carbon-calibration/runs/{run_id}`. Predictions are keyed by model name/version, year and artifact checksum; reruns reuse matching values and refuse to overwrite different values under the same identity. Admin UI then pairs the stored predictions against field calibration/validation plots and saves the resulting assessment automatically; SOC requires an explicit matching depth, and time-gap/coverage exclusions remain visible. The automatic assessment does not create a calibration candidate; run the manual assessment after all required batches are present when fitting a calibration candidate.

Plot polygons in `carbon_field_plots.geometry_normalized` are used when available. Older observations without a polygon use a circular buffer with area equal to the recorded plot area and center at its coordinates. Run metrics disclose which observations lacked valid pixels and include a checksum of the generated prediction snapshot. This provides automatic prediction pairing for eligible saved GEE ensembles; it does not create field observations, validate unapproved models, or establish representativeness. Jobs currently use the carbon-calibration in-process executor, so operators must monitor/reconcile queued or running rows after a process restart.

### Reports from saved analysis jobs

`POST /api/reports/analysis-jobs/{job_id}/executive-summary` creates a DOCX from the persisted successful result and server provenance. It enforces job ownership and feature access and refuses client-supplied replacement metrics. The carbon workspace exposes this report per completed analysis job; its existing complete PDF export remains available. No report approval/version workflow is implied by this endpoint.

### Pre-deploy recovery bundle

The production deploy step now refuses to continue when PostgreSQL is not running or the pre-deploy custom-format dump fails its non-empty and `pg_restore --list` checks. It atomically writes the dump, archives `var/dmrv/evidence` when present, and records SHA-256 checksums plus the deployed application commit in a manifest. These artifacts are kept under the sibling `private-backups` directory with restrictive permissions. This step does not encrypt artifacts, schedule off-host copies, or prove restoreability; do not treat it as a completed disaster-recovery control until encrypted storage, retention, and an isolated restore drill are configured and recorded.

### Operations not verified here

DVC/MLflow, the GEE field extraction route, report download, production database migrations, live inference, scheduled backup, and restore are not exercised in this change. A DVC dataset, private remote and MLflow tracking service must be configured by the operator. The pre-deploy bundle checks structural readability and checksums only; no successful production restore drill is claimed.
