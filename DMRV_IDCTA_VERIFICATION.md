# IDCTA implementation verification

Verified on 14 November 2026 against the integrated workspace, analysis and MFA changes.

## Implemented scope

1. Evidence readiness checklist with independent review and revision history.
2. Membership-scoped project portfolio, search, pagination and CSV export.
3. Opt-in scheduled satellite risk scans, field risk reports and follow-up.
4. Five-pool stock-change scenario accounting with pinned methodology and boundary.
5. Internal registry reconciliation, serial conflict and unique overlap screening.
6. CSV field validation, buffered spatial holdout calibration and block bootstrap.
7. Safeguard evidence and grievance lifecycle.

Additional support includes proof uploads, SHA-256 manifests, review ZIP exports,
owner settings and membership revocation. No sidebar entry was added.

## Results

| Check | Result |
|---|---|
| dMRV, organizations, workspace analysis, MFA and calibration review tests | 251 passed; 4 intentionally skipped SQLite-only concurrency cases |
| PostgreSQL integrity migration | Upgrade, 4 RLS tables, backend write and downgrade passed |
| Full migration chain in an isolated schema | Empty to head; downgrade to 0045; upgrade to 0051 passed |
| Frontend dMRV, organizations, MFA and profile tests | 61 passed in 16 files |
| TypeScript | `tsc --noEmit` passed |
| ESLint for IDCTA and its route/page integrations | Passed |
| Ruff critical/unused checks for new backend code | Passed |
| Vite production build | Passed; existing large bundle warnings remain |
| Actual Earth Engine provider smoke | Passed previously; results in `work/idcta/LIVE_RISK_SMOKE.json` |
| Installed local API and SPA | Health OK; unauthenticated portfolio 401; SPA routes 200 |
| Actual HTTP to local PostgreSQL | Authenticated portfolio/workspace 200; draft save/read and ZIP manifest verification passed |
| Local database migration | 0045 to 0051 applied after backup; existing row counts preserved; 4 RLS tables confirmed |

The actual HTTP workflow used a disposable user and project, without bypassing MFA
on an existing account. Its data was removed after the check. The frontend's new
modules also transformed successfully through the live Vite server. One local API,
frontend and dMRV worker were started. Logs and process IDs are in `work/idcta`.

The PostgreSQL tests used uniquely named temporary schemas, not application tables.
Each schema was removed after the run. The suite checks that organization or project
membership revocation blocks portfolio, workspace and export access; an author cannot
review their own evidence; stale writes fail; corrupt history blocks export; and
satellite failures remain retryable without fabricated zero-risk evidence.

## Limits and remaining release checks

- Native/browser automation was unavailable in this session. Real browser visual QA
  and interactive login were not completed. Component tests are not a substitute.
- Registry data is entered with source evidence. No external registry synchronization
  or credit issuance connector is implemented or implied.
- Accounting is a configurable screening calculation, not a certified methodology
  engine. Internal acceptance does not establish credit eligibility or issue credits.
- Spatial validation metrics apply to the uploaded pairs and their provenance;
  calibration does not automatically replace production models.
- Existing unrelated edits in the original `main` working trees are preserved.
  The 37 installed source/guide files, database migration and local runtime checks
  are recorded in separate receipts under `work/idcta`. The committed feature
  branch `dmrv-idcta-integrity` was copied back to both original local repositories.
  No push to internet remotes or production deployment was performed. The original
  `main` working trees contain concurrent edits and have not been merged or cleaned.

See `DMRV_IDCTA_GUIDE.md` for instructions, formulas and an illustrative case study.

## Supplemental advanced review features — 21 November 2026

On branch `dmrv-idcta-advanced`, the workspace now includes:

- A version-matched VM0047 v1.1 evidence checklist with approach-specific items.
- Snapshot-linked, user-declared satellite source/quality metadata, including
  aerosol QA, cloud cover, observation gaps, dates, season, resolution, and
  processing version.
- An append-only internal ledger for reported issuance, transfer, use, retirement,
  and cancellation events, with evidence, replay protection, and reported-balance
  checks.
- Reviewer triage using open satellite/field risk reports, spatial-validation
  bootstrap intervals, field plot/GPS QA, and uncertainty-input completeness.
- An IndexedDB mobile field capture queue with geotagged timestamp, optional photo,
  project-boundary and registered-plot checks, and retry-safe synchronization.

Current validation for this supplement: TypeScript, ESLint on changed frontend
files, Python syntax parsing, Ruff, service-worker syntax, whitespace checks, and
Vite production build passed. The Vite build reported existing large-bundle and
ineffective dynamic-import warnings. Automated test suites were not run. Migration
`0052_dmrv_advanced_review_tools` is prepared but has not been applied to an
application database; no authenticated HTTP/browser workflow, external registry
sync, merge to `main`, or remote push was performed for this supplement.

The methodology pack is a limited review aid, not an exhaustive VM0047 checker.
The satellite metadata is user-entered and not independently checked against
providers. Credit events and balances remain internally reported, with no official
registry feed or issuance. Offline observations are kept per account/project in
the browser; open the project field tab while online before going offline, and
keep that page available while recording.
