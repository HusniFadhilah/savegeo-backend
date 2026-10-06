# Viewer feature access

Viewer access is stored as the seven `app.feature.*` permissions on the shared
`viewer` role. This applies to both public application users and Admin accounts
whose assigned role is `viewer`. Feature checks run in the API on each request;
the frontend uses the same codes to select routes, tabs, and the no-features
page.

## Who can change the settings

The built-in `admin` role receives the narrow `viewer_features.manage`
permission. Full Access administrators (accounts without a limited role) can
also manage the settings. Viewer and other limited roles do not receive this
permission. It does not grant access to GEE credentials, API keys, or secrets.

## Local migration and seed

After configuring a local/test database, run:

```powershell
alembic upgrade head
python -m scripts.seed_rbac
```

The migration creates missing feature permissions and initializes Viewer with
all seven features once. Seeding is idempotent and keeps the saved Viewer
selection, including an intentionally empty selection. Do not point local
verification at a production or shared database.

## Verification

Sign in as a limited Admin with `viewer_features.manage` to edit the Viewer
settings and verify that GEE credential and secret endpoints still return
403. Then sign in as a Viewer admin and as a public application user: both
should receive the same feature list from `GET /api/app/features`. A disabled
feature must be denied by its API even when its route is opened directly. With
all seven disabled, the dashboard should direct the Viewer to `/no-features`.
