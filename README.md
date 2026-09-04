# SAVEGEO / GEOMOKA Backend (FastAPI + Supabase Postgres)

Rebuild of the legacy Flask backend (`../../backend`) as a modular FastAPI application.
Same API contracts, cleaner structure, Postgres (Supabase) instead of SQLite.

The legacy Flask backend at `../../backend` is untouched and still runnable independently.

## Architecture

```
app/
  main.py              FastAPI app, lifespan (GEE init at startup), CORS, error handlers
  core/                settings (pydantic-settings), JWT/password security, CORS setup
  db/                  SQLAlchemy 2.x engine/session (psycopg3, sync) + 7 models
  schemas/             Pydantic request/response models (used where the legacy contract
                       is simple; loosely-typed legacy passthrough bodies stay as dict)
  api/
    router.py          aggregates every domain router under /api
    deps.py             shared dependencies (require_ee)
    routes/             one file per domain (health, admin, chat, carbon, vegetation, ...)
  services/            business logic (DB + GEE + HTTP calls), one file per domain
  repositories/         DB-only helper queries shared across services/routes
  registries/           static dataset/index/layer catalogs, ported ~verbatim from legacy
  inference/            carbon model loading + prediction (GEE and non-GEE paths)
  providers/            external data sources: ArcGIS, STAC (Planetary Computer),
                       external raster COGs, local raster/grid utilities
  reporting/            DOCX executive-summary generation
  agentic/               AI controller/planner + rate limiter
  static/geo/           island + Indonesia boundary GeoJSON (served by /api/regions/*)
alembic/                DB migrations (one initial revision covering all 7 tables)
scripts/                seed_admin.py, seed_config.py, import_legacy_models.py
tests/                  pytest suite (DB-dependent tests skip gracefully if unreachable)
```

Routes are **sync `def`** (FastAPI runs them in a threadpool) rather than `async def`,
matching the blocking nature of `earthengine-api`, `scikit-learn`, and `rasterio` — none
of which have async APIs. No benefit to `asyncpg`/async routes here; psycopg3 sync is used
consistently throughout.

## Setup

```bash
cd savegeo/backend
pip install -e .          # or: pip install -e ".[dev]" for pytest/ruff/mypy
cp .env.example .env       # then fill in real values
```

The default backend install supports Python 3.11, matching the production
Docker image. Optional Copernicus CDSE ingestion via GeoSave Engine can be
installed separately with `pip install -e ".[cdse]"` in a Python 3.12+
environment.

### Chloris carbon stock data

Chloris AGB stock is available as the `CHLORIS_AGB_STOCK` carbon reference
dataset. Keep Chloris login credentials out of source code and `.env`; use API
credentials/download settings from the Chloris profile or reporting unit:

```bash
# Easiest: direct downloadable GeoTIFF URL for the stock product
CHLORIS_AGB_STOCK_URL=https://...

# Or resolve from a Chloris reporting-unit data folder
CHLORIS_DATA_PATH=s3://chloris-app-data/...

# Or resolve dataPath via the Chloris API
CHLORIS_ORGANIZATION_ID=...
CHLORIS_REPORTING_UNIT_ID=...   # optional when the organization has one unit
CHLORIS_ID_TOKEN=...
```

Check resolution with:

```bash
curl "http://localhost:8086/api/carbon/chloris/status?product=stock&year=2025"
```

### Connecting to Supabase Postgres

1. Create a Supabase project (or use an existing one).
2. Project Settings -> Database -> Connection string -> URI. Copy it into `.env` as:
   ```
   DATABASE_URL=<supabase-postgres-uri-with-psycopg-driver>
   ```
   (Note the `+psycopg` driver suffix — required, do not use the bare `postgresql://` URI
   Supabase gives you directly.)
3. (Optional, only needed for GEE credential upload) Project Settings -> API -> copy
   `Project URL` and `service_role` key into `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY`.
   In Storage, create a **private** bucket named `gee-credentials` (Public: OFF) — or
   whatever you set `SUPABASE_GEE_CREDENTIALS_BUCKET` to.

**Security note on GEE credentials:** the service-account JSON key is uploaded through the
admin panel straight into that private Supabase Storage bucket; only metadata (project id,
client email, bucket path) is stored in Postgres — never the raw key. The
`SUPABASE_SERVICE_ROLE_KEY` bypasses Row Level Security for the whole project's storage,
so treat it like a root credential: keep it server-side only, rotate it if ever exposed.

### Running migrations

```bash
alembic upgrade head
```

### Seeding

```bash
# Admin user (no hardcoded default credential — you must supply one)
SEED_ADMIN_USERNAME=<admin-username> SEED_ADMIN_PASSWORD=<admin-password> python -m scripts.seed_admin

# Default system_config rows (rate limits, analysis defaults, AI provider config, etc.)
python -m scripts.seed_config

# Optional: import legacy saved_models/*.pkl into the uploaded_models table
python -m scripts.import_legacy_models /path/to/old/backend/saved_models
```

### Running the dev server

```bash
uvicorn app.main:app --reload --port 8086
```

Then check:
- `http://localhost:8086/docs` — interactive OpenAPI docs
- `http://localhost:8086/api/health`
- `http://localhost:8086/api/basemaps` — should list Satellite first (`is_default: true`)
- `http://localhost:8086/api/landcover/datasets`, `/api/carbon/datasets`

### Tests

```bash
pytest
```

Registry tests (`tests/test_registries.py`) run without any external dependency.
DB-dependent tests (`test_health.py`, `test_admin_auth.py`) call `pytest.skip()` if
`DATABASE_URL` isn't reachable — they were not exercised against a live Supabase instance
in this environment (no Supabase project credentials were available during this migration).

## Endpoint coverage

All 71 contract endpoints (44 public + 22 admin + 5 chat) listed in the migration task are
implemented and wired into `app/api/router.py`. See the migration summary delivered
alongside this codebase for the full checklist and any noted deviations.

Endpoints intentionally **not** carried over (outside the required contract, present in the
legacy admin panel but not requested for this migration): company-boundary admin CRUD
(create/update/delete + OSM/GFW import), the OpenRouter model-list proxy, and the AI
key-pool status endpoint. Public company read endpoints (`GET /api/companies`,
`GET /api/companies/{id}/geojson`) are implemented.

## Known gaps / follow-ups

- **No live Supabase project was available during this migration** — `alembic upgrade
  head`, real GEE credential upload/activation, and the DB-dependent tests could not be
  exercised end-to-end. Everything was validated via `python -m compileall`, a full
  `app.main:app` import (all 71 routes register correctly), and `TestClient` smoke tests
  against DB-free routes (`/docs`, `/api/landcover/datasets`, `/api/vegetation/catalog`,
  `/api/map-layers`, `/api/basemaps` all return 200; DB-dependent routes fail *gracefully*
  with a caught 500 when no database is reachable, rather than crashing the process).
- `system_config.value` still stores AI provider API keys as plaintext (matches legacy
  behavior) — moving these to Supabase Vault or a dedicated secrets manager is a good
  follow-up, not done here to keep behavioral parity with the source.
- In-memory caches (`_CLIP_POLY_CACHE`, region children-geometry cache, rate limiter
  counters) are module-level dicts, same as legacy — fine for a single-process deployment,
  but won't be consistent across multiple uvicorn workers without moving to Redis.
- `training/` (model retraining scripts) was intentionally **not** copied — it's not a
  runtime dependency of this API. Use the original `backend/training/` scripts if you need
  to retrain a model, then upload the resulting `.pkl`/`.json` pair via
  `POST /api/admin/models/upload`.
