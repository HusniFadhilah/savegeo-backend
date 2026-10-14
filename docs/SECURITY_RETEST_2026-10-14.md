# Security retest: 14 October 2026

## Confirmed finding and containment

The production JWT signing secret was a 32-character value also present in
publicly downloadable frontend vendor assets. The public asset response was
checked without printing the value. This is a signing-key configuration flaw,
even though the application did not deliberately export the key to the client.

The production signing key was replaced atomically with an independently generated
`secrets.token_urlsafe(64)` value and only the API container was recreated. A valid
pre-rotation user token returned HTTP 200 before rotation and HTTP 401 afterward.
A token signed with the replacement key returned HTTP 200. The replacement key
did not occur in any deployed frontend JavaScript file. Existing sessions and
password-reset links require reauthentication. The local development key was
also replaced; neither key value is recorded here or committed.

After containment PostgreSQL still held 2 users, 8 admins, 41 disaster events and
869,439 wildfire observations. Alembic remained at `0032_session_revocation`.

## Prevention

- Production rejects signing keys shorter than 64 characters, simple repeated
  values, provider-token-shaped values and reuse of another configuration value.
- Settings validation errors suppress input values to avoid printing secrets.
- Before starting the new API image, the deployment mounts the frontend assets
  read-only and fails if its signing key occurs in any public HTML, JavaScript,
  CSS, JSON or source-map file. Missing or empty frontend directories also fail.
- App/disaster authentication accepts cookies and Authorization headers, not JWTs
  in URL query parameters. Browser map requests already use same-origin cookies.
- New reset emails put the one-use token in the URL fragment, which is not sent
  in the HTTP request. The frontend retains it only in component memory and
  replaces the address immediately. Legacy query links remain readable and are
  scrubbed too. Successful reset clears the retained token.
- Frontend referrer metadata sends only the origin (`strict-origin`), preserving
  domain-restricted browser map keys while excluding URL paths and queries.
  API Referrer-Policy uses `no-referrer`.

Deploy the compatible frontend before the backend starts sending fragment links.

## Verification evidence

- 118 backend regression tests passed against isolated test databases: logout
  replay, reset replay, malformed/legacy JWTs, namespace separation, foreign chat
  access/mutations, URL-token rejection, CSRF, SSRF, model-upload safety, safe
  upstream error messages and signing-key configuration/exposure checks.
- 11 focused frontend tests passed, including fragment and legacy reset-link
  scrubbing while retaining the token for the reset request.
- Backend Ruff, mypy (61 source files), and workflow actionlint passed locally.
- Frontend production build and focused ESLint checks passed locally.
- 38 public HTTP checks covered both production hostnames. Protected account,
  admin-config/credential, chat, agent and wildfire archive routes rejected missing
  or invalid sessions with HTTP 401. Hostile-origin login returned HTTP 403.
  Protected responses had `Cache-Control: no-store`. Sensitive file probes did
  not expose environment files or repository metadata; the frontend returned its
  SPA HTML fallback and the backend returned HTTP 404.
- Read-only checks with two active production user identities returned HTTP 200
  for their own identity and chat list, HTTP 401 for admin identity access, and
  HTTP 404 for foreign-owned and legacy-unowned chat sessions. Response bodies
  and temporary audit tokens were not recorded.
- An exact-value scan for six configured server credentials found no replacement
  signing key or provider-key values in 367 deployed frontend files, eight archive
  manifests, or the most recent 10,000 API log lines. HTTP responses also had no
  matching server credentials. The one distinct JWT-shaped literal in frontend
  bundles matched the installed Cesium library's public default token.

## Limits

These checks do not prove the absence of every vulnerability or historic leak.
Authenticated mutation tests run in isolated databases; the production ownership
checks were read-only. Historical logs outside the scanned window, third-party
provider account restrictions, and infrastructure outside the app were not audited.
The 7 October integration report documented an earlier FIRMS MAP_KEY traceback
exposure; the owner confirmed on 14 October that this provider key was rotated.
The existing scikit-learn advisory exception remains until legacy model artifacts
are migrated/retrained, as described in the earlier remediation report.
