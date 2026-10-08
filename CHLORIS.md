# Chloris 2024 and 2025

SAVEGEO supports exact annual Chloris stock rasters, including 2024 and 2025.
The dataset picker offers only years present in configured dated URLs or in the
licensed reporting unit's `downloads.json`. The public Planetary Computer
collection continues to cover 2003–2019 at approximately 4.6 km.

The official [Chloris product](https://www.chloris.earth/screening-and-monitoring)
advertises annual 30 m data through 2025. Access to newer rasters depends on
your organization's product and raster download license. A reporting unit
covers its licensed site, rather than providing unrestricted global coverage.

## Configure a licensed source

Add one of these options to **backend `.env`**, then restart the backend.
Keep provider credentials in backend configuration.

1. **Dated GeoTIFF/COG URLs:** set `CHLORIS_STOCK_2024_URL` and/or
   `CHLORIS_STOCK_2025_URL` to the real stock rasters for those years.
2. **Annual URL template:** set `CHLORIS_STOCK_URL_TEMPLATE` to a COG URL
   containing `{year}`, and `CHLORIS_STOCK_YEARS=2024,2025` to the years actually
   available. Missing intervening years are excluded from the picker.
3. **Reporting unit:** set `CHLORIS_DATA_PATH`, or set
   `CHLORIS_ORGANIZATION_ID` and `CHLORIS_REPORTING_UNIT_ID` together with
   `CHLORIS_REFRESH_TOKEN` or `CHLORIS_ID_TOKEN`. The account must have GeoTIFF
   download rights. The backend reads the official `downloads.json` index and
   selects the entry whose product is `stock` and whose date matches the year.

A legacy fixed URL (`CHLORIS_STOCK_URL` or `CHLORIS_AGB_STOCK_URL`) must declare
its true vintage using `CHLORIS_STOCK_YEAR`. An undated fixed URL is never
relabeled as the requested year. Dated URLs take priority over the reporting
unit for the same year. The public fallback is attempted only for the exact
requested public vintage, never as a replacement for 2024/2025.

Set `CHLORIS_STOCK_RESOLUTION_M` to the licensed raster's actual resolution
(default 30 m; use 10 for a licensed 10 m product). It controls catalog metadata
and the AOI statistics sampling grid. Configure one reporting unit/product
resolution consistently.

## Loading and diagnostics

- The authenticated `GET /api/carbon/chloris/status?year=2025` reports public
  and licensed available years and source type. It does not return private
  raster URLs or provider credentials.
- The normal carbon dataset picker automatically exposes 2024/2025 after the
  configuration is loaded. The download index is cached for five minutes;
  failed index requests are cached for 30 seconds.
- Global layer loading and licensed AOI statistics run through rasterio on
  the backend, so licensed Chloris does not require Earth Engine. The map is
  limited to the source raster's spatial coverage.
- Licensed map tiles use authenticated
  `/api/carbon/chloris/reference-tiles/{z}/{x}/{y}.png?year=2025` requests.
  Provider tokens remain server-side, and tiles have private browser caching.
- Stock rasters must contain aboveground **biomass in t/ha**, as defined by
  [Chloris raster documentation](https://app.chloris.earth/docs/1.6.1/05-geospatial-data.html).
  SAVEGEO multiplies stock by 0.47 to display **Mg C/ha**. Do not configure an
  already converted carbon raster as a biomass stock raster. The coarser public
  collection has a separate tonnes-per-pixel conversion.
- Chloris credentials are attached only to the configured Chloris HTTPS origin.
  A raster copied to your own storage needs a URL accessible to the backend,
  such as a valid signed COG URL. GeoTIFFs should support HTTP range reads.
- A year missing from the license/index yields an actionable error. No raster
  from another year is silently substituted. Carbon totals use covered valid
  raster pixels instead of extrapolating a licensed site's density over an
  uncovered AOI.

Adding this integration does not acquire a Chloris license or provision data.
Without a licensed source the picker remains limited to public 2003–2019 data.
