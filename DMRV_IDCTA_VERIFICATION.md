# dMRV/IDCTA implementation and release verification

Status checked on 22 November 2026 against GitHub `main` and the production
deployment workflow.

## Branch and release status

- GitHub `main` is at commit `75d96041dba3cd1d3bcf12a0ece99750e6dd18b6`
  (`fix(dmrv): avoid reserved SQLAlchemy registry attribute`). The local
  `main` checkout matches that commit and has no uncommitted changes.
- The local `dmrv-idcta-integrity` and `dmrv-idcta-advanced` branch tips are
  ancestors of `main`; their changes are included.
- GitHub `feat/dmrv-field-data` and `feat/dmrv-jobs` are behind `main` and have
  no commits absent from it. The one commit unique to
  `fix/dmrv-runtime-rls-20261121` has the same migration as
  `0063_dmrv_runtime_rls.py` in `main`. The one commit unique to
  `fix/prod-dmrv-demo-assets-20261121` has the same two demo-data `COPY`
  instructions already present in the production `Dockerfile`.
- The [CI and production deployment run](https://github.com/HusniFadhilah/savegeo-backend/actions/runs/44997038724)
  completed successfully for the same `main` commit. The full backend suite
  reported **1,254 passed and 11 skipped**. Lint, type checks, dependency
  audit, Gitleaks, and Trivy also passed.
- Production migration advanced from `0064_merge_0063_heads` to
  `0065_merge_all_schema_heads`. The migration-preservation check passed.
  Deployment checked readiness and health, verified the live container
  revision against the expected commit, and recorded the deployment.

## Implemented IDCTA workflows

1. Evidence readiness checklist, safeguards, independent review, and revision
   history.
2. Membership-scoped portfolio, search, pagination, and CSV export.
3. Scheduled/manual satellite risk scans, field reports, follow-up, and
   reviewer prioritization.
4. Five-pool stock-change scenario accounting with methodology/boundary
   context, uncertainty deduction, and buffer inputs.
5. Internal registry/serial records, conflict checks, and geometry-overlap
   screening.
6. CSV field-data validation, spatial holdout calibration, and block
   bootstrap intervals.
7. Safeguard evidence and grievance review/closure.

Additional project-workspace features include a version-matched VM0047 v1.1
review checklist, snapshot-linked satellite provenance and aerosol QA fields,
an append-only reported-credit event ledger, risk-priority triage, and an
offline mobile field-capture queue with GPS/plot checks.

## Scope and known limits

- These workflows organize evidence and review; they do not establish
  methodology eligibility, validation, verification, certification, or credit
  issuance.
- The VM0047 checklist is a concise review aid, not an exhaustive or
  authoritative methodology engine.
- Satellite quality/aerosol metadata is entered by project users. SaveGeo does
  not independently verify it against providers, correct imagery for aerosol,
  or calculate an automatic aerosol-error adjustment.
- Credit events and balances are internally reported. There is no external
  registry feed, registry synchronization, or credit-issuance connector.
- Accounting and risk priority are screening aids based on documented inputs;
  they do not automatically determine carbon losses, reversal, buffer changes,
  or credit claims.
- Spatial-validation metrics describe the uploaded observation/prediction
  pairs. Calibration does not automatically replace a production model.
- Offline field observations remain in the originating browser's local queue
  until synchronized. Keep the project page available and do not clear browser
  storage while captures are pending.

The successful pipeline verifies automated backend tests, migrations, security
checks, and production health for this commit. It is not a substitute for a
human review of project evidence or a visual/browser acceptance test of every
frontend flow.
