# SAVEGEO Backend Operations Runbook

## Local workflow

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
