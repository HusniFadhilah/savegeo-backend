# Wildfire CAMS / FIRMS integration report — 2026-10-07

## Implemented

- Added server-only ADS URL/key/licence-attestation settings, an allowlisted CDS API transport wrapper, and a published-event capability route for CAMS.
- Added product metadata for GFAS v1.2/v1.4.2 and IFS-CAMS. The detail page shows a collapsed, bilingual product panel after server event confirmation.
- Added UTC/product/version validation, a provider protocol, a fixture-testable summary adapter, WGS84 geodesic AOI clipping, flux-to-mass integration, FRP density-to-watts aggregation, nodata handling, and a stable cache digest.
- Added a summary route that validates requests and returns a structured unavailable response until the provider can serve validated CAMS fields. No live data or synthetic CAMS values are displayed; no tile endpoint is exposed without a validated raster.
- Changed NASA FIRMS upstream exception handling to suppress request exception causes, which can contain a key-bearing URL. Added regression coverage for Area and WMS failures.
- No database migration is needed. No commits or pushes were made.

## Upstream and archive status

- Official ADS metadata lists GFAS v1.2 for 2003-2025 and v1.4.2 from 2026 onward, at 0.1 degrees, with hourly and rolling 24-hour fields for v1.4.2. Official IFS-CAMS metadata lists a 0.4-degree global forecast initialized twice daily with up to five days of forecast. ADS requires an account and acceptance of each dataset's terms before retrieval.
- CAMS live validation is **BLOCKED**: the local backend had no ADS key or accepted-terms setting. The raster reader, live fetch, map tiles, animation, and region totals therefore remain disabled.
- The [official NASA FIRMS data-availability API](https://firms.modaps.eosdis.nasa.gov/api/data_availability/) reported the current NRT source products available through **2026-10-06** when checked on 2026-10-07. The development database already held `kalimantan-2026` records through that date (`last_data_at=2026-10-06T13:27Z`; 237,396 published hotspots).
- A one-day idempotent overlap refresh for 2026-10-06 was attempted but failed at the network socket before the sync reached its database commit. Stored rows and sync metadata did not change; `last_data_at`/`last_synced_at` are not being reported as newly refreshed.
- The failed FIRMS request exposed the configured NASA MAP_KEY inside a traceback URL. The traceback propagation is now suppressed and regression-tested. **Rotate/revoke that key in the NASA FIRMS account before further synchronization.** The key value is intentionally omitted here. No repeat upstream request was made after the exposure.

## Verification

- Backend focused pytest: **33 passed** across CAMS metadata/math/adapter, event routes, FIRMS error redaction, and wildfire region route tests. Backend Ruff: **passed**.
- Frontend focused Vitest: **5 passed** across the CAMS panel and event confirmation guard. Frontend production build: **passed**.
- Frontend ESLint: **exit 0, 0 errors, 29 warnings** across the repository. i18n audit: **exit 0, 0 fatal issues** (1,097 candidate hardcoded-text warnings are reported by the existing audit).
- The build reports existing dynamic-import/chunk-size advisories. No browser staging E2E was run because there is no configured CAMS field or staging fixture endpoint; UI behavior is covered by component tests. Live ADS and NASA sync validation remain blocked until credentials are available and external access is allowed.

## Deployment steps

1. Rotate the exposed NASA FIRMS key in the NASA account; update backend secret storage.
2. Provision an ADS account/key, accept GFAS and IFS dataset terms, and set the server-side CAMS settings.
3. Implement and validate the bounded ADS retrieval, normalized raster archive, cache, and tile provider before enabling summary/tile responses.
4. Run staged comparisons against official ADS files and Fire Emissions Watch on matching variables, times, versions, units, and regions; record numeric tolerances and provenance.
5. Only after those checks, add the interactive map layer, legend, time slider/playback, and regional emissions charts.
