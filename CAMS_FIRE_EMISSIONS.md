# CAMS fire emissions and smoke forecast

## Data products

SaveGeo's wildfire detail page now has a collapsed CAMS product panel and a server capability endpoint. The panel distinguishes two official products:

| Product | Meaning | Metadata shown |
| --- | --- | --- |
| GFAS | Fire-source emission estimates derived from satellite fire radiative power (FRP), dry matter consumption, and species emission factors. | v1.2 daily, 2003-01-01 to 2025-12-31; v1.4.2 hourly estimates and rolling 24-hour averages from 2026-01-01; 0.1-degree grid. |
| IFS-CAMS | Atmospheric composition analysis/forecast from multiple emissions and assimilation inputs. It is not fire-only attribution or a surface station measurement. | Forecast cycles at 00 and 12 UTC, up to 120 hours; 0.4-degree grid. Single-level PM2.5 is hourly (kg m-3); carbon monoxide is a model-level mixing ratio (kg kg-1) at 3-hour steps, using the lowest model level when a near-surface field is required. |

The catalogue includes GFAS CO2, CO, PM2.5, overall burnt-carbon flux, and FRP density. Entries are displayed as catalogue metadata only. The actual variable list and availability must still be checked for each requested version and time in the retrieved file.

## Access and configuration

The backend uses these server-only settings, also listed in `.env.example`:

```dotenv
CAMS_ADS_API_URL=https://ads.atmosphere.copernicus.eu/api
CAMS_ADS_API_KEY=
CAMS_ADS_TERMS_ACCEPTED=false
```

Create an ADS account, install the official CDS API client with `pip install -e ".[cams]"` following the [ADS API setup guide](https://ads.atmosphere.copernicus.eu/how-to-api), open each dataset's terms page, and accept the applicable licence before requesting data. Set `CAMS_ADS_TERMS_ACCEPTED=true` only after the operator has completed that step. Keep the key in backend secret storage. Do not put it in the frontend, logs, fixture data, or reports.

At the time of this integration, the repo had no CAMS key and no recorded licence acceptance. Even after those settings are supplied, the capability endpoint intentionally reports live validation as blocked: no ADS-backed raster ingestion/tiling provider has been connected or verified. Setting a key alone cannot enable an overlay.

`CdsApiArchiveAdapter` wraps the documented `cdsapi.Client.retrieve(dataset_id, request, target)` contract and allowlists the two ADS dataset IDs. It accepts request fields produced from the official dataset form; it does not invent a dataset request schema. Its transport call is mock-tested and sanitizes provider errors. The `CamsFieldProvider` interface and `summarize_request` are fixture-tested separately. The live summary route validates the event and query, then returns a structured HTTP 503 until a provider can verify downloaded field provenance, units, nodata, and geometry. No tile route is exposed while there is no validated raster output. Do not scrape Fire Emissions Watch or use its private endpoints as a substitute.

## Units and aggregation

For a GFAS emission flux, the mass for one interval and clipped region is:

```text
mass_kg = sum(flux_kg_m-2_s-1 × geodesic_cell_area_m2 × interval_seconds)
```

The cell area uses WGS84 geodesic area after intersecting the cell with the requested AOI. A daily mean uses its documented daily interval; an hourly field uses its hourly interval. Do not add rolling 24-hour means across adjacent valid times as if they were independent hourly emissions because the windows overlap. FRP is a density (W m-2): the region total is density multiplied by clipped cell area (W), with no mass or time integration.

The calculator preserves separate maximum and mean flux per valid cell and total integrated mass. It ignores nodata rather than converting it to zero. Missing-only summaries return `null` for numeric statistics. It rejects negative emission flux, invalid duration, bad geometry, and overflow. Confidence filters from NASA FIRMS do not apply to CAMS.

Store timestamps as offset-aware UTC. GFAS is not merged across versions or different step lengths without explicit normalization. Keep provider, dataset/version, variable, valid time, source unit, geometry revision, retrieval timestamp, and query digest with any future raster cache.

## API and current UI behavior

- `GET /api/disasters/wildfires/events/{slug}/emissions/capabilities` requires the existing wildfire feature permission and resolves a published wildfire event first.
- `GET /api/disasters/wildfires/events/{slug}/emissions/summary` applies the same checks, validates product/version/variable/time, and returns a structured 503 while the provider is not ready.
- The frontend waits for the event lookup to confirm a server-backed event. The panel is collapsed by default and requests capabilities only when expanded. It does not request summaries or tiles while `data_available` is false.
- Since there is no verified CAMS raster yet, the panel shows product metadata, units, provenance links, and the blocker. It does not show a colored overlay, numerical legend, time slider, playback, or region statistics that could be mistaken for real data.

## Official references

- [Fire Emissions Watch methodology](https://atmosphere.copernicus.eu/fire-emissions-watch-global-wildfire-emission-data-your-fingertips)
- [GFAS dataset catalogue](https://ads.atmosphere.copernicus.eu/datasets/cams-global-fire-emissions-gfas)
- [IFS-CAMS atmospheric composition forecast catalogue](https://ads.atmosphere.copernicus.eu/datasets/cams-global-atmospheric-composition-forecasts)
- [ADS/CDS API setup](https://ads.atmosphere.copernicus.eu/how-to-api)
- [GFAS v1.4.2 documentation](https://confluence.ecmwf.int/spaces/CKB/pages/601301528/CAMS+global+biomass+burning+emissions+based+on+fire+radiative+power+GFAS+data+documentation)
- [GFAS data access transition notice](https://forum.ecmwf.int/t/changes-to-cams-global-biomass-burning-emissions-based-on-fire-radiative-power-gfas-data-availability-on-ads-and-ecmwf-data-portal/14325)

Metadata checked on 2026-10-07. Dataset licences are CC-BY; follow the licence page for required attribution and dataset-specific terms.
