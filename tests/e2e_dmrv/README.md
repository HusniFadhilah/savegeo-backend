# dMRV end-to-end checks (live stack, manual)

These scripts drive a **running** stack: they are not collected by `pytest` and must only ever be pointed at a
**throwaway** database. `seed_throwaway_db.py` refuses any `DATABASE_URL` that is not `127.0.0.1:55433/e2e`.

## 1. Throwaway PostgreSQL (example, Windows/XAMPP binaries)

```text
initdb -D <dir> -U postgres -A trust -E UTF8
pg_ctl -D <dir> -o "-p 55433 -c listen_addresses=127.0.0.1" -l <dir>/pg.log start
psql -h 127.0.0.1 -p 55433 -U postgres -c "CREATE DATABASE e2e"
```

## 2. Backend on that database

```text
DATABASE_URL=postgresql+psycopg://postgres@127.0.0.1:55433/e2e
JWT_SECRET_KEY=<any throwaway value>   APP_ENV=development
python -m alembic upgrade head
PYTHONPATH=. python tests/e2e_dmrv/seed_throwaway_db.py     # RBAC + accounts owner-a, owner-b, field-user, stranger
python -m uvicorn app.main:app --host 127.0.0.1 --port 8086
```

## 3. Frontend (Vite dev server proxies `/api` to 8086)

```text
cd ../frontend && npx vite --host 127.0.0.1 --port 5501 --strictPort
```

## 4. Run

| Script | What it proves |
| --- | --- |
| `run_api_checks.py` | 31 checks on the real server: cookies, CSRF middleware, uniform 404, idempotent replay, membership, boundaries and overlap, field import with the Bu Dessy sample, upload limit, `If-Match`. |
| `run_browser_flow.py` | 25 checks in headless Chromium (Python Playwright): login, create project (double click = one project), boundary errors and versions, import preview/commit, density with area basis, non-member and field-operator views, English UI, 390 px layout. Writes `shots/` and `browser_e2e_report.json`. |
| `run_import_benchmark.py` | 20 000 and 50 000 row imports, 1 % bad rows in partial mode, and the 50 001-row rejection. |

Data is made unique per run, so the scripts can be repeated against the same database.
The password in these scripts belongs to the throwaway accounts only.
