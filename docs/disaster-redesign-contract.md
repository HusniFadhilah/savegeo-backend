# Disaster Intelligence Dashboard — API Contract

Foundation (models, migration, registry, repo, analysis service) is already built and
live-verified against real Postgres + real GEE. This doc is the single source of truth
for the 4 pieces being built in parallel against it: admin routes, user routes/auth,
admin frontend, user frontend. **Do not deviate from these shapes** — the frontend
agents are coding against this doc, not against the backend agents' actual code.

## Already-built foundation (read, do not modify unless told)

- `app/db/models/{user,disaster_event,disaster_aoi,satellite_imagery,analysis_run,analysis_result,hotspot}.py`
  — each has `.to_dict()`. `AnalysisResult.to_dict(include_features=False)` — pass
  `include_features=True` only when the caller actually needs the GeoJSON.
- `app/registries/disaster_model_registry.py` — `list_models(enabled_only=False)`,
  `get_model(model_id)`, `is_enabled(model_id)`. 7 entries, 3 `enabled: True`
  (`flood_change_v1`, `water_segmentation_v1`, `forest_change_v1`), 4 `enabled: False`.
  Each entry has `user_label` (what User-facing UI must display) vs `backend_label`
  (internal, Admin/metadata only).
- `app/repositories/disaster_repo.py` — full CRUD helpers, already covers everything
  both routers need (events, AOI, imagery, runs, results, publish/unpublish, hotspots).
  Read its docstrings/signatures before writing a new query — it almost certainly
  already has the function you need.
- `app/services/disaster_analysis_service.py` — `run_analysis(db, run_id) -> {"run":..., "result":...}`.
  Raises `AnalysisError` (from `app.services.gee_common`, has `.status_code`) on bad
  input or missing data — catch it like every other analyze endpoint in this codebase
  (`except AnalysisError as e: raise HTTPException(status_code=e.status_code, detail=str(e))`).
- `app/services/disaster_cross_layer_service.py` — `get_available_cross_layer_stats(db, event_id) -> list[dict]`.
  Only ever returns flood×forest (or empty list) — never fabricate building/road entries.
- `app/services/geo_utils.py` — `estimate_area_ha(geojson)`, `bbox_and_centroid(geojson) -> (bbox, centroid)`.
- `app/core/security.py` — `get_current_admin`, `get_current_user` (NEW — for the new
  `users` table), `require_permission(code)`, `hash_password`/`verify_password`,
  `create_access_token(admin)`, `create_user_access_token(user)` (NEW).
- `app/services/audit_service.log_audit(db, admin_user_id, action, resource_type, resource_id, detail=None)`
  — call after every admin mutation. **Always use `resource_type="disaster_event"` and
  `resource_id=str(event_id)`** regardless of which sub-entity actually changed (AOI/
  imagery/run/result/hotspot) — this is what lets a single query reconstruct one
  event's full audit trail. Put the specifics in `detail` (e.g. `{"field": "status", "from": "draft", "to": "processing"}`).
- New RBAC permission codes already seeded (`scripts/seed_rbac.py`): `disaster.create`,
  `disaster.update`, `disaster.delete`, `disaster.aoi.write`, `disaster.imagery.write`,
  `disaster.analysis.configure`, `disaster.analysis.run`, `disaster.analysis.publish`,
  `disaster.analysis.unpublish`, `disaster.result.write`, `disaster.result.delete`.

## MVP scope reality check (important for both frontend agents)

None of the 3 real models produce per-object `features` (no building/road detection
exists) — `AnalysisResult.features` is always `null` for MVP. This means:
- Feature-level filtering (spec §25 "Impact Level", "Building Type", etc.), click-a-building/
  road for detail (§22/§23), and the `/disasters/:id/features` endpoint are **structurally
  inapplicable** for these 3 models. `/disasters/:id/features` still exists (returns an
  always-empty FeatureCollection) so the route shape is future-proof, but **frontend must
  not build filter UI that implies per-object data exists** — no "Impact Level" dropdown,
  no feature click-popup, for these 3 models. Only whole-layer toggle + area-based
  statistics apply.
- Cross-layer stats: only flood×forest is ever returned. Render "Not Available" (or hide)
  for any other combination — never compute one client-side.
- Disabled models (`enabled: false`) must render "Not Available" and never show a Run/
  checkbox-enabled control anywhere in User UI.

## Response envelope convention (existing backend-wide rule)

Success → payload directly on 200. Failure → `{"error": "..."}` (or FastAPI's default
`{"detail": "..."}` via `HTTPException`) on non-2xx. **Never** wrap success in
`{success, data}` — a past session bug (see repo history) came from a frontend that
assumed that shape against this exact backend.

---

## A. Admin routes — `app/api/routes/admin_disaster.py`, prefix `/admin/disasters`

**Do not register this router yourself** — central wiring happens in
`app/api/router.py` (`api_router.include_router(...)`, see the existing list there for
every other domain router) and will be done in the integration pass after both A and B
land, to avoid two agents editing that shared file concurrently. Just define
`router = APIRouter(prefix="/admin/disasters", tags=["admin-disaster"])` in your own
file and leave it there. Verify your file in isolation:
`python -c "from app.api.routes.admin_disaster import router; print(len(router.routes))"`.

Every route: `Depends(get_current_admin)`. Every **mutating** route additionally:
`Depends(require_permission("disaster.xxx"))` per the table below, and an
`audit_service.log_audit(...)` call after commit.

| Method & path | Permission | Body / Query | Returns |
|---|---|---|---|
| `POST /admin/disasters` | `disaster.create` | `{name, disaster_type, location_name?, province?, district?, event_date?, start_date?, end_date?, severity?, description?, source?, thumbnail?}` | created event `.to_dict()` |
| `GET /admin/disasters` | (read, no permission gate) | `?status=&disaster_type=&severity=&year=&search=` | `{events: [...]}` — all statuses (unlike User's published-only list) |
| `GET /admin/disasters/{id}` | (read) | — | `{event, aoi, imagery: {pre:[...], post:[...]}, runs: [{run, result}]}` — `result` included even if unpublished (Admin sees everything); 404 if event doesn't exist (no published filter here) |
| `PATCH /admin/disasters/{id}` | `disaster.update` | any subset of event fields incl. `status` | updated event `.to_dict()` |
| `DELETE /admin/disasters/{id}` | `disaster.delete` | — | `{"message": "deleted"}` |
| `POST /admin/disasters/{id}/aoi` | `disaster.aoi.write` | `{geojson, source?}` — compute `area_ha` via `estimate_area_ha`, `bbox`/`centroid` via `bbox_and_centroid` before calling `disaster_repo.create_aoi` | created AOI `.to_dict()` |
| `GET /admin/disasters/{id}/imagery` | (read) | `?phase=pre|post` | `{imagery: [...]}` |
| `POST /admin/disasters/{id}/imagery` | `disaster.imagery.write` | `{phase, satellite, acquisition_date, sensor?, resolution_m?, cloud_coverage_pct?, data_source?, is_primary?}` | created imagery `.to_dict()` |
| `POST /admin/disasters/{id}/imagery/{imagery_id}/primary` | `disaster.imagery.write` | — | updated imagery `.to_dict()` |
| `GET /admin/disasters/models` | (read) | — | `{models: list_models(enabled_only=False)}` — for the Analysis Manager's model picker |
| `POST /admin/disasters/{id}/analyses` | `disaster.analysis.configure` | `{model_id, aoi_id, pre_imagery_id?, post_imagery_id?}` — validate `model_id` exists in registry (404 if not) but do **not** require `enabled` here (creating the run row is fine even if disabled; running it is where `enabled` is enforced, inside `disaster_analysis_service`) | created run `.to_dict()` |
| `GET /admin/disasters/{id}/analyses` | (read) | — | `{runs: [{run, result}]}` all statuses |
| `POST /admin/analyses/{run_id}/run` | `disaster.analysis.run` | — | `disaster_analysis_service.run_analysis(db, run_id)` result, catch `AnalysisError` |
| `PATCH /admin/analyses/{run_id}/status` | `disaster.analysis.run` | `{status}` | updated run `.to_dict()` |
| `POST /admin/analyses/{run_id}/publish` | `disaster.analysis.publish` | — | `disaster_repo.publish_result(...)` result `.to_dict()`; 404 if no result exists yet for this run |
| `POST /admin/analyses/{run_id}/unpublish` | `disaster.analysis.unpublish` | — | result `.to_dict()` |
| `PATCH /admin/analyses/{run_id}/result` | `disaster.result.write` | partial `{statistics?, legend?, confidence_summary?}` overrides | updated result `.to_dict()` |
| `DELETE /admin/analyses/{run_id}/result` | `disaster.result.delete` | — | `{"message": "deleted"}` |
| `GET /admin/disasters/{id}/qc` | (read) | — | `{aoi_configured, pre_imagery_available, post_imagery_available, analyses: [{model_id, status, has_statistics, has_legend, has_confidence}], ready_to_publish}` — booleans computed from the event's current AOI/imagery/runs |
| `GET /admin/disasters/{id}/hotspots` | (read) | — | `{hotspots: [...]}` all (published + unpublished) |
| `POST /admin/disasters/{id}/hotspots` | `disaster.result.write` | `{name, impact_level, geojson, analysis_result_id?, stats?, is_published?}` | created hotspot `.to_dict()` |
| `PATCH /admin/hotspots/{hotspot_id}` | `disaster.result.write` | partial fields incl. `is_published` | updated hotspot `.to_dict()` |
| `DELETE /admin/hotspots/{hotspot_id}` | `disaster.result.delete` | — | `{"message": "deleted"}` |
| `GET /admin/disasters/{id}/audit` | (read) | — | `{logs: [...]}` — `AuditLog` rows where `resource_type="disaster_event" AND resource_id=str(id)`, newest first |

File boundary: only create `app/api/routes/admin_disaster.py` + register it in `app/main.py`
(one import + one `include_router` line, matching the existing pattern for `admin.py`).
Do not touch `app/api/routes/admin.py` itself.

---

## B. User auth + user disaster routes

### `app/api/routes/user_auth.py`, prefix `/auth`

New `app/schemas/user.py` with `UserRegisterRequest{username, email, password}` and
`UserLoginRequest{username, password}` (pydantic, mirror `app/schemas/admin.py`'s
`LoginRequest`).

| Method & path | Body | Returns |
|---|---|---|
| `POST /auth/register` | `{username, email, password}` | `{token, user}` — 409 if username/email taken; `password` min 8 chars (400 otherwise); hash via `hash_password` from `app.core.security`, issue via `create_user_access_token` |
| `POST /auth/login` | `{username, password}` | `{token, user}` — 401 on bad creds, mirrors `admin.py`'s `login()` exactly (update `last_login`) |
| `GET /auth/me` | — (`Depends(get_current_user)`) | `user.to_dict()` |

### `app/api/routes/disaster_events.py`, prefix `/disasters` (NOT `/disaster` — that's the
existing legacy router, left alone, see below)

Every route: `Depends(get_current_user)`. Every event lookup **must** filter
`status == "published"` (via `disaster_repo.list_events(published_only=True)` /
manually check `event.status == "published"` after `get_event`) — return 404 (not 403)
for an existing-but-unpublished event, so User routes never leak that a draft exists.

| Method & path | Query | Returns |
|---|---|---|
| `GET /disasters` | `?disaster_type=&year=&province=&severity=&search=` | `{events: [event.to_dict() + {"available_analysis_count": N}]}` — count published analyses per event via `disaster_repo.list_published_analyses` |
| `GET /disasters/{id}` | — | `{event, aoi (read-only, no write fields), imagery: {pre:[...], post:[...]}, primary_imagery: {pre: {...}|null, post: {...}|null}}` — 404 if not published |
| `GET /disasters/{id}/analyses` | — | `{analyses: [{model_id, user_label, category, available: bool, run: {...}|null, result: {...}|null}]}` — one entry per **enabled** registry model; `available=True` only if a published result exists for that model on this event; disabled models are omitted entirely (not listed as unavailable — per spec §17 "hide or Not Available", hiding is simpler and this doc picks hiding for disabled models, while enabled-but-not-yet-run-for-this-event models ARE listed with `available:false` so User sees "Not Available" rather than the item vanishing) |
| `GET /disasters/{id}/layers` | — | `{satellite: {pre_tile_url, post_tile_url}, analyses: <same shape as /analyses>}` |
| `GET /disasters/{id}/statistics` | — | `{kpis: {...flattened from each published result's statistics, namespaced by model_id...}, cross_layer: get_available_cross_layer_stats(...)}` |
| `GET /disasters/{id}/hotspots` | — | `{hotspots: [...published only...]}` |
| `GET /disasters/{id}/features` | `?analysis=&bbox=` | `{features: {"type": "FeatureCollection", "features": []}, note: "Per-feature detail is not available for this event's analyses yet."}` — always this shape for MVP (see contract intro), still a real endpoint so frontend can call it without special-casing |

File boundary: create `app/api/routes/user_auth.py` (`router = APIRouter(prefix="/auth", tags=["user-auth"])`),
`app/api/routes/disaster_events.py` (`router = APIRouter(prefix="/disasters", tags=["disasters"])`),
`app/schemas/user.py`. **Do not register either router in `app/api/router.py`** — same
reason as section A, done centrally in the integration pass. Verify in isolation:
`python -c "from app.api.routes.user_auth import router as r1; from app.api.routes.disaster_events import router as r2; print(len(r1.routes), len(r2.routes))"`.
**Do not modify**
`app/api/routes/disaster.py` (legacy BMKG/DEM/sources router) except exactly this:
add `Depends(get_current_user)` to its 3 remaining routes (`/sources`, `/bmkg-alerts`,
`/dem-slope`) and **delete** the `POST /disaster/event-map` route + its
`get_disaster_event_map` call (superseded by the new model-based analysis run flow) —
leave `disaster_service.get_disaster_event_map` function itself alone in
`disaster_service.py` (dead code is fine to leave, don't go refactor that file further).

---

## C. Admin frontend — `features/admin/components/DisasterManagement.tsx` + subcomponents

New tab "Disaster Management" registered in `AdminDashboard.tsx`'s existing tab list
(follow the exact pattern the other tabs use — read `AdminDashboard.tsx` first).

Sub-components (own files under `features/admin/components/disaster/`):
- `DisasterEventList.tsx` — table of all events (any status), reuse the datatables/
  pagination convention `CompanyBoundaries.tsx` already uses for a big admin list.
  Row actions: Edit, Delete, Open (goes to detail/review view).
- `DisasterEventForm.tsx` — Event Information fields (name/type/dates/province/
  district/severity/status/description) per spec §3, create or edit mode.
- `AoiManager.tsx` — draw via the existing `AoiDrawingTools` component
  (`components/map/AoiDrawingTools.tsx`, props `{onChange, externalGroupRef?}`) +
  GeoJSON file upload input; shows computed area/centroid/bbox from the AOI-save
  response; POSTs to `/admin/disasters/{id}/aoi`.
- `ImageryManager.tsx` — pre/post tabs, list + add form (satellite/date/cloud%/
  resolution), "Set as Primary" button per row, per spec §4.
- `AnalysisManager.tsx` — model picker sourced from `GET /admin/disasters/models`
  (shows disabled ones grayed out with the registry's `description` explaining why),
  "Run" button per attached analysis, status badges (Queued/Processing/Completed/
  Failed/Review Required/Published) per spec §47 example.
- `AnalysisReview.tsx` — map (reuse `MapView` + a plain tile layer for `result.tile_url`)
  + statistics readout + QC checklist (`GET /admin/disasters/{id}/qc`) + Publish/
  Unpublish/Re-run buttons.
- `DisasterAuditTrail.tsx` — simple list reading `GET /admin/disasters/{id}/audit`.

`features/admin/api.ts` gets new functions for every endpoint in section A above
(`auth: true` on all, matching every other admin api call in that file).

File boundary: only add files under `features/admin/components/disaster/`, extend
`features/admin/api.ts` and `features/admin/types.ts` (additive only), and the minimal
tab-registration edit to `AdminDashboard.tsx`. Do not touch any other admin component.

---

## D. User frontend — replaces `features/disaster/`

- `features/disaster/DisasterListPage.tsx` (NEW, replaces the old `DisasterModule.tsx`
  as the module's entry) — published event cards + filter bar (`disaster_type`, `year`,
  `province`, `severity`) + search, per spec §11. Calls `GET /disasters`.
- `features/disaster/DisasterDashboard.tsx` (NEW) — single event view, structure per
  spec §14/§48:
  1. Header (name/type/date/location/severity/description) from `GET /disasters/{id}`.
  2. Satellite viewer: reuse `SwipeCompareMap.tsx` (props: `beforeUrl/afterUrl/
     beforeLabel/afterLabel/orientation/...`) for Swipe, and the split-view pattern from
     `features/lc-change/components/BeforeAfterMaps.tsx` for Side-by-Side; Pre/Post-only
     just show one `MapView` with one tile layer. Date pickers are **plain dropdowns**
     populated only from `imagery.pre`/`imagery.post` arrays already returned by the
     event-detail response — never a free date input.
  3. Analysis selector: checkboxes from `GET /disasters/{id}/analyses`, disabled +
     "Not Available" label when `available:false`, human `user_label` only — never
     show `model_id`.
  4. KPI tiles from `GET /disasters/{id}/statistics` (`kpis`), per spec §13 examples.
  5. Map: `MapView` + one tile layer per checked analysis (`result.tile_url`), dynamic
     `MapLegend` (already generic, props `{title?, entries}`) driven by the checked
     analysis' `result.legend`.
  6. `LayerPanel.tsx` (NEW) — structure per spec §29 (Satellite / Change Detection /
     Segmentation / Reference groups) — wires visibility, doesn't fetch anything itself.
  7. `StatisticsPanel.tsx` (NEW) — chart.js, follow `TimeSeriesChart.tsx`'s registration
     pattern (`ChartJS.register(...)`), render whatever's in `kpis` per analysis category
     (bar/pie as appropriate — use judgement, spec §32 lists example chart types).
  8. `HotspotPanel.tsx` (NEW) — list from `GET /disasters/{id}/hotspots`, Zoom/Highlight/
     View Detail buttons only (no edit).
  9. Cross-layer stat card: render only entries `GET /disasters/{id}/statistics` actually
     returns in `cross_layer` — if empty, omit the section, don't render a placeholder.
- Keep `features/disaster/components/{BmkgAlerts,DemSlopeControls,SourceStatusPanel}.tsx`
  (and their `api.ts` calls) working, folded into a collapsed "Additional Sources"
  section at the bottom of `DisasterDashboard.tsx` — **add `auth: true`** to those 3
  api calls (`fetchDisasterSources`, `fetchBmkgAlerts`, `analyzeDemSlope` in
  `features/disaster/api.ts`) since the backend now gates them behind `get_current_user`.
  Delete the old `analyzeDisasterEvent` call + its `EventSummary`/`DisasterEventMap`/
  `DisasterControlsPanel` components (superseded — event-map route is being removed).

### New auth pieces (own files, don't touch `authService.ts`/`useAuthStore.ts` — those
stay admin-only)
- `services/userAuthService.ts` — mirror `services/authService.ts` exactly but
  `TOKEN_KEY="savegeo_user_token"`, `USER_KEY="savegeo_user_user"`, posts to
  `/auth/login` / `/auth/register` (not `/admin/auth/login`).
- `hooks/useUserAuthStore.ts` — mirror `hooks/useAuthStore.ts`, register its own
  401 handler (check `apiClient.ts` — the 401 handler is a single global slot; if it's
  genuinely single-slot, make the registered handler dispatch based on which token kind
  the failing request used, or route both handlers through one function that clears
  whichever token is present — read `apiClient.ts` first and pick whichever keeps admin
  and user sessions from clobbering each other).
- `pages/LoginUserPage.tsx` / `RegisterUserPage.tsx` — styled like the existing admin
  `LoginPage` for visual consistency.
- `routes/AppRoutes.tsx` — add `/pemetaan-bencana` (List) and
  `/pemetaan-bencana/:eventId` (Dashboard), gated inline exactly like `AdminPage.tsx`
  gates `/admin` today (`isAuthenticated` from `useUserAuthStore` → else render
  `LoginUserPage`). Leave the existing `/` `DashboardPage` module-tab system (`carbon`,
  `lc-change`, etc.) untouched — only these two new routes are gated.

File boundary: only files under `features/disaster/`, the 4 new files above
(`userAuthService.ts`, `useUserAuthStore.ts`, `LoginUserPage.tsx`, `RegisterUserPage.tsx`),
and the additive route registration in `AppRoutes.tsx`. Do not touch `features/admin/`,
`authService.ts`, or `useAuthStore.ts`.

---

## Verification each agent must run before reporting done

- Backend (A, B): `python -m compileall app/` clean, then `python -c "from app.main import app; print(len(app.openapi()['paths']))"` to confirm the app still imports with the new routers mounted.
- Frontend (C, D): `npx tsc --noEmit` clean (not `npm run build`/`npm run dev` — avoids dist/port races with parallel agents).
