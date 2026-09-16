# SAVEGEO Backend

Backend API SAVEGEO berbasis FastAPI, SQLAlchemy, PostgreSQL, Google Earth Engine, dan layanan analisis geospasial.

Runbook deployment dan operasi tersedia di [OPERATIONS.md](OPERATIONS.md).

## Prasyarat

- Python 3.11
- PostgreSQL 15 atau lebih baru
- GDAL/rasterio sesuai sistem operasi
- Git
- Akses Google Earth Engine untuk analisis yang membutuhkannya

## Instalasi Lokal

```powershell
cd savegeo/backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
Copy-Item .env.example .env
```

Isi `.env` sesuai database dan layanan yang digunakan. File `.env` tidak boleh di-commit.

## Environment Variables

| Variable | Keterangan |
|---|---|
| `APP_ENV` | `development` atau `production` |
| `DEBUG` | Gunakan `false` pada production |
| `DATABASE_URL` | PostgreSQL URI dengan driver `postgresql+psycopg://` |
| `JWT_SECRET_KEY` | Secret acak untuk token autentikasi |
| `API_PREFIX` | Default `/api` |
| `PORT` | Default `8086` |
| `ALLOWED_ORIGINS` | Origin frontend dipisahkan koma |
| `FRONTEND_BASE_URL` | URL frontend aktif |
| `GEE_SERVICE_ACCOUNT` | Email service account Earth Engine |
| `GEE_KEY_FILE` | Path credential Earth Engine |
| `GEE_PROJECT_ID` | Google Cloud project Earth Engine |
| `SUPABASE_URL` | URL Supabase, jika digunakan |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side only |
| `UPLOAD_DIR` | Folder upload |
| `MODEL_DIR` | Folder model |
| `DISASTER_RASTER_DIR` | Folder raster dan thumbnail bencana |

Gunakan `.env.example` sebagai daftar lengkap variabel yang didukung.

## Database

```powershell
alembic upgrade head
python -m scripts.seed_config
python -m scripts.seed_rbac
```

Buat akun admin menggunakan environment sementara:

```powershell
$env:SEED_ADMIN_USERNAME = "admin"
$env:SEED_ADMIN_PASSWORD = "gunakan-password-kuat"
python -m scripts.seed_admin
Remove-Item Env:SEED_ADMIN_USERNAME, Env:SEED_ADMIN_PASSWORD
```

## Menjalankan Backend

```powershell
uvicorn app.main:app --reload --host 127.0.0.1 --port 8086
```

Endpoint pemeriksaan:

- `http://localhost:8086/`
- `http://localhost:8086/api/health`
- `http://localhost:8086/docs`

## Data Bencana dan Aset Raster

- Metadata event, AOI, imagery, dan analisis disimpan di PostgreSQL.
- Raster disimpan di `DISASTER_RASTER_DIR`.
- Thumbnail disimpan di `DISASTER_RASTER_DIR/thumbnails`.

Metadata dan aset harus dipindahkan bersama. Path `local_file_path` wajib menunjuk ke path server.

## Testing dan Quality Gate

```powershell
ruff check . --ignore E501 --ignore B904 --ignore B905
mypy app
bandit -r app -lll
pip-audit . --ignore-vuln PYSEC-2024-110
pytest
```

GitHub Actions juga menjalankan Gitleaks dan Trivy sebelum deployment.

## Deployment

- `.github/workflows/deploy-production.yml`: server production.
- `.github/workflows/ci.yml`: validasi Pull Request.
- `.github/workflows/code-review.yml`: Alibaba OpenCodeReview.

Deployment berjalan setelah lint, typecheck, security audit, test, Gitleaks, dan Trivy berhasil. Secret deployment disimpan di GitHub Actions Secrets.

## Struktur Utama

```text
app/api/          Route FastAPI
app/core/         Konfigurasi dan security
app/db/           Model dan session database
app/repositories/ Query database
app/services/     Business logic dan integrasi eksternal
app/providers/    Provider imagery dan raster
alembic/          Database migration
scripts/          Seed, ingest, dan utilitas operasional
tests/            Unit dan integration tests
var/              Data runtime, upload, model, raster, dan job
```

## Troubleshooting

- API `401`: pastikan bearer token valid.
- API `500`: periksa `DATABASE_URL`, migration, dan credential layanan.
- Thumbnail/raster `404`: periksa `DISASTER_RASTER_DIR`, path database, dan proxy `/disaster-thumbnails/`.
- GEE tidak aktif: periksa service account, key file, project ID, dan permission Earth Engine.
- Deployment gagal: periksa gate pertama yang merah di GitHub Actions.
