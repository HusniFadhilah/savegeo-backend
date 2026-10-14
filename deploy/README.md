# SAVEGEO production deployment

Production layout:

- frontend static files: `/home/ubuntu/savegeo/frontend/dist`
- backend source/config: `/home/ubuntu/savegeo/backend`
- FastAPI: Docker, exposed on server port `8086` for the LEN reverse proxy
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
$knownHosts = ssh-keyscan -p 6970 192.168.17.55 2>$null | Out-String

foreach ($repo in $repos) {
  $vpnConfigB64 | gh secret set VPN_CONFIG_B64 --repo $repo
  $vpnAuthB64 | gh secret set VPN_AUTH_B64 --repo $repo
  $knownHosts | gh secret set SSH_KNOWN_HOSTS --repo $repo
  gh secret set SSH_PASSWORD --repo $repo
}
```

The final command prompts for the SSH password for each repository without
putting it in shell history.

## Deployment behavior

A push to `main` runs tests first, connects the GitHub runner to the VPN, and
then deploys:

- backend workflow syncs source while preserving server `.env` and `var/`,
  creates a pre-deploy DB dump, rebuilds containers, and checks `/api/health`;
- before starting the API, deployment mounts the public frontend assets read-only
  and rejects a JWT signing key that occurs in any HTML, JavaScript, JSON or source
  map. Production also rejects short, repeated, provider-token and reused keys.
  Generate the server-only key with `python -c "import secrets; print(secrets.token_urlsafe(64))"`.
  Rotating it invalidates existing sessions and reset links; account data remains intact.
- when `AI_PROVIDER=ollama`, the backend deployment probes Ollama from inside
  the running API runtime and verifies that `AI_MODEL` is installed;
- frontend workflow builds with `VITE_API_BASE_URL=/api`, syncs only `dist/`,
  and performs an HTTP smoke test.

Backend provenance JSON is always preserved in GitHub job summaries. Artifact
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
```

Create a manual database backup:

```bash
cd /home/ubuntu/savegeo/backend
docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U savegeo -Fc savegeo \
  > "var/exports/manual_$(date +%Y%m%d_%H%M%S).dump"
```

Do not commit `.env`, VPN credentials, SSH passwords, GEE keys, database dumps,
or model archives.
