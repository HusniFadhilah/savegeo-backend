# Disaster multiclass comparison

## Implemented behavior

The disaster registry now exposes the already available Dynamic World V1 land-cover model output as a nine-class comparison option. Class IDs, labels and colors come from the existing land-cover registry. This uses published per-scene inference from GEE, not a newly trained disaster-damage model. SAM's class-agnostic objects and the existing SAR/NDWI/NDVI methods are not relabelled as trained multiclass damage predictions.

Model recommendations consider the event type, enabled implementation, configured GEE credentials, version, source sensor and available pre/post records. Compatible multiclass output takes priority; no benchmark-based accuracy ranking is invented. Unsupported local BlackSky uploads are rejected, not silently replaced with Sentinel-2.

Both imagery IDs must belong to the event and correct phase, and the AOI must belong to that event. The current imagery table stores acquisition dates, not exact source scene IDs: prediction selection is explicitly restricted to each configured calendar date and AOI; all selected source IDs and counts are returned. No date-window fallback is performed. Missing predictions fail clearly. Both sides must have the same inference and QA versions. Categorical labels use nearest-neighbour on the same explicit 10 m UTM grid and the intersection of valid pixels. Statistics and all tile outputs use that same AOI/grid/mask.

Outputs include pre/post class tiles, changed pixels colored by destination class, post confidence tiles, class area/pixel/percentage/probability statistics, per-class gains/losses/unchanged area and a transition table. Probabilities describe land-cover classes, not confidence in disaster damage. Cloud/no-data is reported separately. Low common coverage (<50%) or low mean class probability (<0.5) produces `review_required` with reasons. Empty/invalid class rasters cannot succeed.

Comparison metadata lives inside the existing JSONB statistics column and is exposed separately as `comparison` plus key top-level fields, so no schema migration is needed. Execution transitions queued -> processing -> completed/review_required/failed; publishing requires a finished result. Recomputed results become unpublished. Failures are logged and persisted. Result creation precedes marking completion.

Caching uses event, AOI, pre/post IDs, model/version, dates and AOI geometry. GEE URL reuse is bounded to six hours. Review's explicit rerun sends `force=true` to refresh tiles. Concurrent execution of the same processing run is rejected. No map interaction calls an analysis endpoint.

Admin review and the public dashboard share `SegmentationComparison`: Pre, Post, Berdampingan, Geser (vertical/horizontal), Perubahan and Segmentasi multi-kelas. Split maps synchronize center/zoom; swipe uses one existing `SwipeCompareMap`. AOI borders remain above segmentation. Class colors and statistics remain constant across modes.

## Validation evidence, 14 September 2026

- Production frontend build passed. Source frontend unit suite: 9 passed after retrying a Vitest worker-start timeout with `--maxWorkers=1`.
- Backend compile passed. Final source regression suite: 58 tests + 8 subtests passed, including 23 segmentation tests.
- Browser tests used actual GEE model tiles: desktop/mobile, both orientations, all six modes, nine legend rows, fixed-location pixel changes, synchronized split zoom, stable map coordinates and no analysis requests during interaction. Screenshots are in ignored `frontend/test-results/segmentation/`.
- Existing slider regression completed the shared/disaster/land-cover/carbon combinations. An imagery mobile scene-picker timeout was rerun successfully with imagery/resolution (8 combinations).
- Live Bali AOI [115.13, -8.72, 115.28, -8.55], pre 2024-06-15 and post 2025-08-04: inference version **3.5**, QA version **1**, 2 pre scenes / 4 post scenes. AOI 31,171.68 ha; common valid area 10,138.13 ha; changed class area 1,564.12 ha. All nine classes have real output pixels. Low coverage and low class probabilities are explicitly flagged for review; these figures are land-cover differences, not attributed disaster damage.
- Small live AOI [115.20, -8.66, 115.23, -8.64] also produced valid output (733.42 ha AOI, 11.87 ha common valid); this illustrates why coverage must be reported rather than filling gaps with fabricated classes.
- Database read-only inventory found 16 flood and 18 earthquake events; no stored landslide, forest-fire or tsunami events. Type compatibility and invalid/missing inputs for these types are covered by automated tests, not claimed as live event validation.
- Real persisted flood event **19**, AOI **18**, imagery **36** (2025-11-03) / **41** (2025-12-18) was executed as unpublished run **63**. GEE has no Dynamic World post prediction for that date/AOI. Run 63 correctly remains **failed**, with the explicit reason and no result/publication. Dates and source imagery were not substituted.

The available model does not provide validated landslide/burned/damaged-building semantic classes or inference on BlackSky uploads. Protected production pages have not been tested through an authenticated deployed session. These are availability/validation limits, not successful damage-model results.

## Reproduction

From backend: `python scripts/verify_disaster_segmentation.py` (configured GEE credentials required). Optional `--pre`, `--post`, `--bbox`, `--output` parameters select explicit inputs. It writes ephemeral tile URLs under ignored frontend test results and does not create/publish event records.

With frontend Vite running: `node tests/browser/segmentation-check.cjs`. Set `PLAYWRIGHT_MODULE` and `SWIPE_TEST_URL` if needed. `tests/test_disaster_segmentation.py` covers registry schemas, missing/foreign/mismatched imagery, invalid area/class output, persisted failure, concurrent-run rejection and reuse without a second inference.

## Files

Backend: disaster model registry; new `disaster_segmentation_service.py`; analysis/cross-layer services; admin/public disaster routes; result serialization/repository; the live verification script and segmentation tests.

Frontend: new `SegmentationComparison.tsx`; dashboard; admin model manager/review/API/types; disaster result types; browser segmentation fixture and checks.

Reference: [Official Dynamic World catalog, class schema, algorithm/QA metadata and source-image mapping](https://developers.google.com/earth-engine/datasets/catalog/GOOGLE_DYNAMICWORLD_V1).
