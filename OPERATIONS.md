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
```

For disaster imagery, database `local_file_path` values must point to the target server. Thumbnail URLs use `/disaster-thumbnails/...` and the web server must proxy that path to FastAPI.

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
