#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

BACKUP_DIR="${SAVEGEO_LOCAL_BACKUP_DIR:-../private-backups}"
POSTGRES_IMAGE="${SAVEGEO_RESTORE_POSTGRES_IMAGE:-postgres:16-alpine}"

for command in docker sha256sum python3; do
  command -v "$command" >/dev/null 2>&1 || { echo "Required restore command is missing: $command" >&2; exit 2; }
done
[[ -d "$BACKUP_DIR" && ! -L "$BACKUP_DIR" ]] || { echo "Local pre-deploy backup directory is missing or unsafe." >&2; exit 3; }

backup_name="$(python3 "${SAVEGEO_BACKUP_SELECTOR:-$(dirname -- "$0")/select_verified_backup.py}" "$BACKUP_DIR")"
[[ -n "$backup_name" ]] || { echo "No verified local pre-deploy database dump is available." >&2; exit 4; }
backup_path="$BACKUP_DIR/$backup_name"
manifest_path="${backup_path%.dump}.manifest"
[[ -f "$backup_path" && ! -L "$backup_path" && -f "$manifest_path" && ! -L "$manifest_path" ]] || {
  echo "The latest pre-deploy dump or its manifest is missing or unsafe." >&2
  exit 5
}

expected_sha="$(python3 - "$manifest_path" <<'PY'
from pathlib import Path
import sys

manifest = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if separator:
        manifest[key] = value
if manifest.get("database_dump") != Path(sys.argv[1]).name.removesuffix(".manifest") + ".dump":
    raise SystemExit("Backup manifest does not identify its database dump.")
value = manifest.get("database_sha256", "")
if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
    raise SystemExit("Backup manifest has an invalid database checksum.")
print(value)
PY
)"
actual_sha="$(sha256sum "$backup_path" | awk '{print $1}')"
[[ "$actual_sha" == "$expected_sha" ]] || { echo "Pre-deploy database checksum mismatch." >&2; exit 6; }

docker image inspect "$POSTGRES_IMAGE" >/dev/null 2>&1 || {
  echo "The restore image is not present locally; refusing a network pull during the drill." >&2
  exit 7
}

container="savegeo-local-restore-$(date +%s)-$$"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --rm --name "$container" --network none \
  -e POSTGRES_PASSWORD=restore-drill-only "$POSTGRES_IMAGE" >/dev/null
for attempt in $(seq 1 60); do
  docker exec "$container" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 2
done
docker exec "$container" pg_isready -U postgres >/dev/null
docker exec "$container" createdb -U postgres savegeo_restore_check
docker exec -i "$container" pg_restore -U postgres -d savegeo_restore_check \
  --no-owner --no-privileges - < "$backup_path"

restored_tables="$(docker exec "$container" psql -U postgres -d savegeo_restore_check -Atc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")"
alembic_version="$(docker exec "$container" psql -U postgres -d savegeo_restore_check -Atc \
  'SELECT version_num FROM alembic_version ORDER BY version_num LIMIT 1')"
[[ "$restored_tables" =~ ^[0-9]+$ ]] && (( restored_tables > 30 )) || {
  echo "Restore drill did not recover the expected application schema." >&2
  exit 8
}
[[ -n "$alembic_version" ]] || { echo "Restored database has no Alembic version marker." >&2; exit 8; }

printf 'Local production backup restore drill passed: backup=%s, alembic=%s, public_tables=%s, sha256=%s\n' \
  "$backup_name" "$alembic_version" "$restored_tables" "$actual_sha"
