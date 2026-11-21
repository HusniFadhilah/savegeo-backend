#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

source "$(dirname "${BASH_SOURCE[0]}")/load_backup_environment.sh"

: "${SAVEGEO_BACKUP_S3_URI:?Set s3://bucket/prefix on the production host}"
: "${SAVEGEO_BACKUP_AGE_IDENTITY_FILE:?Set path to the age private identity file}"
POSTGRES_IMAGE="${SAVEGEO_RESTORE_POSTGRES_IMAGE:-postgres:16-alpine}"
COMPOSE_FILE="${SAVEGEO_BACKUP_COMPOSE_FILE:-docker-compose.prod.yml}"
for command in age aws docker jq sha256sum python3; do
  command -v "$command" >/dev/null 2>&1 || { echo "Required restore command is missing: $command" >&2; exit 2; }
done
[[ -f "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE" && ! -L "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE" ]] || { echo "Age identity file is missing or unsafe." >&2; exit 2; }
[[ "$(stat -c '%a' "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE")" == "600" ]] || { echo "Age identity file must have mode 600." >&2; exit 2; }
aws sts get-caller-identity >/dev/null

bucket_prefix="${SAVEGEO_BACKUP_S3_URI#s3://}"
bucket="${bucket_prefix%%/*}"
key_prefix="${bucket_prefix#*/}"
key_prefix="${key_prefix%/}/"
manifest_key="$(aws s3api list-objects-v2 --bucket "$bucket" --prefix "$key_prefix" --query 'sort_by(Contents[?ends_with(Key, `.manifest.json`)], &LastModified)[-1].Key' --output text)"
[[ -n "$manifest_key" && "$manifest_key" != None ]] || { echo "No backup manifest found at $SAVEGEO_BACKUP_S3_URI." >&2; exit 4; }

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/savegeo-restore-drill.XXXXXX")"
container="savegeo-restore-drill-$(date +%s)-$$"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf -- "$tmp_dir"
}
trap cleanup EXIT
aws s3 cp "s3://${bucket}/${manifest_key}" "$tmp_dir/manifest.json" --only-show-errors
db_key="$(jq -er '.database.key' "$tmp_dir/manifest.json")"
expected_db_sha="$(jq -er '.database.sha256_encrypted' "$tmp_dir/manifest.json")"
aws s3 cp "s3://${bucket}/${db_key}" "$tmp_dir/database.dump.age" --only-show-errors
actual_db_sha="$(sha256sum "$tmp_dir/database.dump.age" | awk '{print $1}')"
[[ "$actual_db_sha" == "$expected_db_sha" ]] || { echo "Encrypted database checksum mismatch." >&2; exit 5; }
docker run -d --rm --name "$container" --network none -e POSTGRES_PASSWORD=restore-drill-only "$POSTGRES_IMAGE" >/dev/null
for attempt in $(seq 1 60); do
  docker exec "$container" pg_isready -U postgres >/dev/null 2>&1 && break
  sleep 2
done
docker exec "$container" pg_isready -U postgres >/dev/null
docker exec "$container" createdb -U postgres savegeo_restore_check
age -d -i "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE" "$tmp_dir/database.dump.age" \
  | docker exec -i "$container" pg_restore --list - >/dev/null
age -d -i "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE" "$tmp_dir/database.dump.age" \
  | docker exec -i "$container" pg_restore -U postgres -d savegeo_restore_check --no-owner --no-privileges
restored_tables="$(docker exec "$container" psql -U postgres -d savegeo_restore_check -Atc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")"
alembic_version="$(docker exec "$container" psql -U postgres -d savegeo_restore_check -Atc 'SELECT version_num FROM alembic_version LIMIT 1')"
[[ "$restored_tables" =~ ^[0-9]+$ ]] && (( restored_tables > 30 )) || { echo "Restore drill did not recover the expected application schema." >&2; exit 6; }
[[ -n "$alembic_version" ]] || { echo "Restored database has no Alembic version marker." >&2; exit 6; }

evidence_state="$(jq -r '.evidence.state' "$tmp_dir/manifest.json")"
if [[ "$evidence_state" == "present" ]]; then
  evidence_key="$(jq -er '.evidence.key' "$tmp_dir/manifest.json")"
  expected_evidence_sha="$(jq -er '.evidence.sha256_encrypted' "$tmp_dir/manifest.json")"
  aws s3 cp "s3://${bucket}/${evidence_key}" "$tmp_dir/evidence.tar.gz.age" --only-show-errors
  actual_evidence_sha="$(sha256sum "$tmp_dir/evidence.tar.gz.age" | awk '{print $1}')"
  [[ "$actual_evidence_sha" == "$expected_evidence_sha" ]] || { echo "Encrypted evidence checksum mismatch." >&2; exit 7; }
  age -d -i "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE" "$tmp_dir/evidence.tar.gz.age" > "$tmp_dir/evidence.tar.gz"
  mkdir -m 700 "$tmp_dir/evidence-restore"
  evidence_entries="$(python3 scripts/safe_extract_backup_tar.py "$tmp_dir/evidence.tar.gz" "$tmp_dir/evidence-restore")"
  [[ "$evidence_entries" =~ ^[0-9]+$ ]] && (( evidence_entries > 0 )) || { echo "Evidence archive restored no entries." >&2; exit 9; }
fi

data_state="$(jq -r '.runtime_data.state // "absent"' "$tmp_dir/manifest.json")"
restored_data_entries=0
if [[ "$data_state" == "present" ]]; then
  data_key="$(jq -er '.runtime_data.key' "$tmp_dir/manifest.json")"
  expected_data_sha="$(jq -er '.runtime_data.sha256_encrypted' "$tmp_dir/manifest.json")"
  aws s3 cp "s3://${bucket}/${data_key}" "$tmp_dir/runtime-data.tar.gz.age" --only-show-errors
  actual_data_sha="$(sha256sum "$tmp_dir/runtime-data.tar.gz.age" | awk '{print $1}')"
  [[ "$actual_data_sha" == "$expected_data_sha" ]] || { echo "Encrypted runtime data checksum mismatch." >&2; exit 8; }
  age -d -i "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE" "$tmp_dir/runtime-data.tar.gz.age" > "$tmp_dir/runtime-data.tar.gz"
  mkdir -m 700 "$tmp_dir/runtime-data-restore"
  restored_data_entries="$(python3 scripts/safe_extract_backup_tar.py "$tmp_dir/runtime-data.tar.gz" "$tmp_dir/runtime-data-restore")"
  [[ "$restored_data_entries" =~ ^[0-9]+$ ]] && (( restored_data_entries > 0 )) || { echo "Runtime data archive restored no entries." >&2; exit 9; }
fi
printf 'Restore drill passed: manifest=%s, alembic=%s, public_tables=%s, evidence=%s, runtime_data=%s entries\n' \
  "$manifest_key" "$alembic_version" "$restored_tables" "$evidence_state" "$restored_data_entries"
