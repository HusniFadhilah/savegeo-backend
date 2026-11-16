# SAVEGEO production deployment

Production layout:

- frontend static files: `/home/ubuntu/savegeo/frontend/dist`
- backend source/config: `/home/ubuntu/savegeo/backend`
- FastAPI: Docker, bound to `127.0.0.1:8086` behind the verified Apache proxy
- PostgreSQL 16: Docker volume, not exposed to the network
- Apache: serves the frontend on ports `80`/`5500` and proxies `/api/` to FastAPI

## GitHub Actions secrets

Authenticate GitHub CLI first:

```powershell
gh auth login -h github.com
```

Set the same secrets on both repositories. These commands keep the VPN files
out of Git and send their contents directly to GitHub Secrets:

```powershell
$repos = @(
  "HusniFadhilah/savegeo-backend",
  "HusniFadhilah/savegeo-frontend"
)

$vpnConfig = "your-vpn.vpn-client-config.ovpn"
$vpnAuth = "your_server.txt"
$vpnConfigB64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($vpnConfig))
$vpnAuthB64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($vpnAuth))
$knownHosts = Get-Content -Raw '<independently-verified-known-hosts-file>'

foreach ($repo in $repos) {
  $vpnConfigB64 | gh secret set VPN_CONFIG_B64 --repo $repo
  $vpnAuthB64 | gh secret set VPN_AUTH_B64 --repo $repo
  $knownHosts | gh secret set SSH_KNOWN_HOSTS --repo $repo
}
```

Create a separate restricted deployment key for each repository. Verify the
server host fingerprint through an independent trusted source, append only the
public key to the deployment account with the `restrict` option, and verify the
new login before switching the workflow. Store each private key in that
repository's `DEPLOY_SSH_PRIVATE_KEY` secret. Keep the private key out of Git,
terminal output and evidence packages. Retire the old Actions `SSH_PASSWORD`
secret only after the replacement workflow has succeeded.

Production requires Docker BuildKit/Buildx. Before rollout, run
`scripts/prepare_runtime_database.py` on the host, check the separate runtime
role, and grant UID 10001 explicit access to the verified `var` mount with ACLs
while preserving host ownership. See `OPERATIONS.md` for the data and model
preflight requirements. Existing pickle models require verified offline ONNX
conversion before the new loader is deployed.

## Deployment behavior

A push to `main` runs tests first, connects the GitHub runner to the VPN, and
then deploys to LEN. Beta Doltinuku deployment is manual and needs its own
authorized maintenance window:

- backend workflow syncs source while preserving server `.env` and `var/`,
  creates a private pre-deploy DB dump, runs migrations separately from the
  restricted API process, rebuilds containers, and checks `/api/ready`;
- before starting the API, deployment mounts the public frontend assets read-only
  and rejects a JWT signing key that occurs in any HTML, JavaScript, JSON or source
  map. Production also rejects short, repeated, provider-token and reused keys.
  Generate the server-only key with `python -c "import secrets; print(secrets.token_urlsafe(64))"`.
  Rotating it invalidates existing sessions and reset links; account data remains intact.
- when `AI_PROVIDER=ollama`, the backend deployment probes Ollama from inside
  the running API runtime and verifies that `AI_MODEL` is installed;
- frontend workflow builds with `VITE_API_BASE_URL=/api`, syncs only `dist/`,
  and performs an HTTP smoke test.

Backend build metadata is always preserved in GitHub job summaries. Artifact
uploads are an additional copy with seven-day retention. If GitHub artifact
storage is unavailable, the deployment job recreates metadata from the exact
validated checkout and GitHub run context; the SHA/environment/workflow checks
remain mandatory. A final deployment-record upload failure is reported as a
warning after the container, Ollama, health, and revision checks have passed.

## Operations

```bash
cd /home/ubuntu/savegeo/backend
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs -f --tail=200 api
curl --fail http://127.0.0.1:8086/api/health
curl --fail http://127.0.0.1:8086/api/ready
```

Create a manual database backup:

```bash
cd /home/ubuntu/savegeo/backend
umask 077
mkdir -p ../private-backups
chmod 700 ../private-backups
docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U savegeo -Fc savegeo \
  > "../private-backups/manual_$(date +%Y%m%d_%H%M%S).dump"
```

Do not commit `.env`, VPN credentials, SSH passwords, GEE keys, database dumps,
or model archives.
