# dMRV end-to-end checks (live stack, manual)

These scripts drive a **running** stack: they are not collected by `pytest` and must only ever be pointed at a
**throwaway** database. `seed_throwaway_db.py` accepts only `127.0.0.1` ports 55433 or 55434 with database `e2e`.

## 1. Throwaway PostgreSQL (example, Windows/XAMPP binaries)

```text
initdb -D <dir> -U postgres -A trust -E UTF8
pg_ctl -D <dir> -o "-p 55434 -c listen_addresses=127.0.0.1" -l <dir>/pg.log start
psql -h 127.0.0.1 -p 55434 -U postgres -c "CREATE DATABASE e2e"
```

## 2. Backend on that database

```text
DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55434/e2e
JWT_SECRET_KEY=<any throwaway value>   APP_ENV=development
DMRV_EVIDENCE_DIR=<throwaway-dir>/evidence
RATE_LIMIT_REQUESTS_PER_MINUTE=1000   # throwaway browser E2E only; never use this as a production limit
python -m alembic upgrade head
PYTHONPATH=. python tests/e2e_dmrv/seed_throwaway_db.py     # RBAC + accounts owner-a, owner-b, field-user, stranger
python -m uvicorn app.main:app --host 127.0.0.1 --port 8086
```

In a separate terminal, start the durable dMRV worker against the same throwaway database:

```text
python -m app.workers.dmrv_worker --poll-interval 0.5
```

## 3. Frontend (Vite dev server proxies `/api` to 8086)

```text
cd ../frontend && npx vite --host 127.0.0.1 --port 5501 --strictPort
```

## 4. Run

| Script | What it proves |
| --- | --- |
| `run_api_checks.py` | Real-server checks for cookies/CSRF, uniform 404, idempotent replay, membership, boundaries/overlap, field import, monitoring period versioning and boundary binding, period-scoped durable job access, upload limit, and `If-Match`. |
| `run_browser_flow.py` | Headless Chromium flow: login, create project (double click = one project), boundary errors/version, monitoring period start, field import/preview/density, worker-backed period snapshot, incomplete evidence ZIP, blocked draft submission, non-member and field-operator views, English UI, 390 px layout. Writes `shots/` and `browser_e2e_report.json`. |
| `run_import_benchmark.py` | 20 000 and 50 000 row imports, 1 % bad rows in partial mode, and the 50 001-row rejection. |

Data is made unique per run, so the scripts can be repeated against the same database.
The password in these scripts belongs to the throwaway accounts only.
