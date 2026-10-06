# Commercial imagery providers

The scene browser keeps open-data catalogs and customer-licensed catalogs as separate providers. Search and preview do not create an order or tasking request.

## Planet commercial

Configure these backend-only settings for a customer STAC endpoint:

```env
PLANET_STAC_URL=https://...
PLANET_STAC_COLLECTION=...
PLANET_API_KEY=...
PLANET_AUTH_SCHEME=basic
```

SAVEGEO requires the catalog URL, collection, and credential before showing Planet commercial search as connected. The provider's STAC service is documented as beta, so the collection, coverage, product metadata, assets, and customer rights must be confirmed against the account. The separate `planet_open_data` provider remains the public disaster-event catalog.

## Airbus OneAtlas Data

Configure the API key and the workspace ID issued to the SAVEGEO customer account:

```env
ONEATLAS_API_KEY=...
ONEATLAS_WORKSPACE_ID=...
```

The adapter uses the documented OneAtlas Data authentication and OpenSearch endpoints; it does not treat OneAtlas as STAC. Search is account-scoped and limited to sensor-level Living Library results. A scene is previewable only when the returned scene grants `rights.wmts`; preview then streams the documented RGB WMTS through the authenticated SAVEGEO backend. A missing WMTS right produces metadata-only results. This connector does not order, purchase, task, or download source products.

OneAtlas references:

- [Authentication](https://api.oneatlas.airbus.com/guides/g-authentication/)
- [Data Search / OpenSearch](https://api.oneatlas.airbus.com/guides/oneatlas-data/g-search/)
- [View service and WMTS](https://api.oneatlas.airbus.com/guides/oneatlas-data/g-service-view/)

## BlackSky Spectra

BlackSky remains visible as a blocked catalog entry. The placeholder settings `BLACKSKY_SPECTRA_API_CONTRACT_REFERENCE` and `BLACKSKY_SPECTRA_CREDENTIAL` do not activate a connector. SAVEGEO needs the official customer API endpoint/schema, authentication requirements, preview rights, and account contract before enabling search or preview; no endpoint or access terms are inferred.

## Safety and UI behavior

- Provider credentials remain in backend settings and are not returned in catalog or scene payloads.
- Commercial STAC scene IDs are opaque, short-lived server references. Asset downloads and tiles resolve them on the backend.
- OneAtlas provider IDs and bearer tokens are kept on the backend; browser tiles use authenticated SAVEGEO routes.
- Commercial providers do not expose unsupported mosaic, analytics, band-selection, or visual-enhancement controls.
- Unknown resolution, revisit interval, and archive start year remain unset until returned by provider scene metadata.
