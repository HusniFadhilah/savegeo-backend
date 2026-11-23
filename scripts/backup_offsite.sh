#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

source "$(dirname "${BASH_SOURCE[0]}")/load_backup_environment.sh"

: "${SAVEGEO_BACKUP_S3_URI:?Set s3://bucket/prefix on the production host}"
: "${SAVEGEO_BACKUP_AGE_RECIPIENT:?Set the age public recipient on the production host}"
RETENTION_DAYS="${SAVEGEO_BACKUP_RETENTION_DAYS:-90}"
COMPOSE_FILE="${SAVEGEO_BACKUP_COMPOSE_FILE:-docker-compose.prod.yml}"
EVIDENCE_DIR="${SAVEGEO_BACKUP_EVIDENCE_DIR:-var/dmrv/evidence}"
DATA_PATHS="${SAVEGEO_BACKUP_DATA_PATHS:-var/saved_models var/uploads var/dmrv/integrity var/disaster_rasters var/landcover-models.json}"

for command in age aws docker sha256sum jq python3 tar; do
  command -v "$command" >/dev/null 2>&1 || { echo "Required backup command is missing: $command" >&2; exit 2; }
done
[[ "$SAVEGEO_BACKUP_S3_URI" == s3://*/* ]] || { echo "SAVEGEO_BACKUP_S3_URI must include a bucket and prefix." >&2; exit 2; }
uri_path="${SAVEGEO_BACKUP_S3_URI#s3://}"
bucket="${uri_path%%/*}"
object_prefix="${uri_path#*/}"
[[ -n "$bucket" && -n "$object_prefix" && "$object_prefix" != "$uri_path" && "$object_prefix" != *".."* ]] || { echo "Backup URI must contain a bucket and non-empty safe prefix." >&2; exit 2; }
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] && (( RETENTION_DAYS >= 30 )) || { echo "Retention must be an integer of at least 30 days." >&2; exit 2; }
if [[ -n "${SAVEGEO_BACKUP_S3_ENDPOINT_URL:-}" ]]; then
  : "${SAVEGEO_BACKUP_S3_ACCESS_KEY_ID:?Set the local S3 access key in the backup environment}"
  : "${SAVEGEO_BACKUP_S3_SECRET_ACCESS_KEY:?Set the local S3 secret key in the backup environment}"
  export AWS_ACCESS_KEY_ID="$SAVEGEO_BACKUP_S3_ACCESS_KEY_ID"
  export AWS_SECRET_ACCESS_KEY="$SAVEGEO_BACKUP_S3_SECRET_ACCESS_KEY"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
  export AWS_EC2_METADATA_DISABLED=true
  [[ -z "${SAVEGEO_BACKUP_AWS_CONFIG_FILE:-}" ]] || export AWS_CONFIG_FILE="$SAVEGEO_BACKUP_AWS_CONFIG_FILE"
  aws_backup() { aws --endpoint-url "$SAVEGEO_BACKUP_S3_ENDPOINT_URL" "$@"; }
  if ! aws_backup s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
    aws_backup s3api create-bucket --bucket "$bucket" --region "$AWS_DEFAULT_REGION"
  fi
  sse_args=()
else
  aws_backup() { aws "$@"; }
  aws sts get-caller-identity >/dev/null
  sse_args=(--sse AES256)
fi
docker compose -f "$COMPOSE_FILE" ps --status running db | grep -q db || { echo "Production database container is not running." >&2; exit 3; }

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/savegeo-backup.XXXXXX")"
trap 'rm -rf -- "$tmp_dir"' EXIT
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
key_base="${object_prefix%/}/savegeo_${stamp}"

docker compose -f "$COMPOSE_FILE" exec -T db pg_dump -U savegeo -Fc savegeo \
  | age -r "$SAVEGEO_BACKUP_AGE_RECIPIENT" -o "$tmp_dir/database.dump.age" -
db_sha="$(sha256sum "$tmp_dir/database.dump.age" | awk '{print $1}')"
db_key="${key_base}.dump.age"
aws_backup s3 cp "$tmp_dir/database.dump.age" "s3://${bucket}/${db_key}" "${sse_args[@]}" --only-show-errors

evidence_state="absent"
evidence_key=""
evidence_sha=""
if [[ -d "$EVIDENCE_DIR" && ! -L "$EVIDENCE_DIR" ]] && find "$EVIDENCE_DIR" -type f -print -quit | grep -q .; then
  evidence_parent="$(cd "$(dirname "$EVIDENCE_DIR")" && pwd -P)"
  evidence_name="$(basename "$EVIDENCE_DIR")"
  # Evidence files may be mode 0600 and owned by the API runtime UID. Read
  # through an isolated, read-only container with DAC read/search override,
  # matching the production deployment archive path, without changing source
  # permissions or exposing the data to a networked container.
  docker run --rm --network none --read-only --user 0:0 \
    --cap-drop ALL --cap-add DAC_READ_SEARCH \
    --volume "$evidence_parent:/source:ro" \
    postgres:16-alpine tar -C /source -czf - "$evidence_name" \
    | age -r "$SAVEGEO_BACKUP_AGE_RECIPIENT" -o "$tmp_dir/evidence.tar.gz.age" -
  evidence_sha="$(sha256sum "$tmp_dir/evidence.tar.gz.age" | awk '{print $1}')"
  evidence_key="${key_base}.evidence.tar.gz.age"
  aws_backup s3 cp "$tmp_dir/evidence.tar.gz.age" "s3://${bucket}/${evidence_key}" "${sse_args[@]}" --only-show-errors
  evidence_state="present"
fi

data_state="absent"
data_key=""
data_sha=""
read -r -a data_paths <<< "$DATA_PATHS"
archive_paths=()
for path in "${data_paths[@]}"; do
  [[ "$path" == var/* && "/$path/" != *"/../"* ]] || { echo "Backup data paths must remain inside the application var directory." >&2; exit 2; }
  [[ ! -L "$path" ]] || { echo "Refusing to archive a symlink in persistent runtime data: $path" >&2; exit 2; }
  [[ -e "$path" ]] && archive_paths+=("$path")
done
if (( ${#archive_paths[@]} )); then
  # Runtime files can be owned by service UIDs with mode 0600. Read only the
  # var directory in an isolated container with DAC read/search override.
  docker run --rm --network none --read-only --user 0:0 \
    --cap-drop ALL --cap-add DAC_READ_SEARCH \
    --volume "$PWD/var:/source/var:ro" \
    postgres:16-alpine tar -C /source -czf - "${archive_paths[@]}" \
    | age -r "$SAVEGEO_BACKUP_AGE_RECIPIENT" -o "$tmp_dir/runtime-data.tar.gz.age" -
  data_sha="$(sha256sum "$tmp_dir/runtime-data.tar.gz.age" | awk '{print $1}')"
  data_key="${key_base}.runtime-data.tar.gz.age"
  aws_backup s3 cp "$tmp_dir/runtime-data.tar.gz.age" "s3://${bucket}/${data_key}" "${sse_args[@]}" --only-show-errors
  data_state="present"
fi

commit="$(git rev-parse HEAD 2>/dev/null || printf 'unknown')"
python3 - "$stamp" "$commit" "$db_key" "$db_sha" "$evidence_state" "$evidence_key" "$evidence_sha" "$data_state" "$data_key" "$data_sha" > "$tmp_dir/manifest.json" <<'PY'
import json,sys
stamp,commit,db_key,db_sha,evidence_state,evidence_key,evidence_sha,data_state,data_key,data_sha=sys.argv[1:]
print(json.dumps({"schema":"savegeo-offsite-backup-v1","created_at_utc":stamp,"commit":commit,
  "database":{"key":db_key,"sha256_encrypted":db_sha,"format":"pg_dump_custom","age_encrypted":True},
  "evidence":{"state":evidence_state,"key":evidence_key or None,"sha256_encrypted":evidence_sha or None,"age_encrypted":evidence_state=="present"},
  "runtime_data":{"state":data_state,"key":data_key or None,"sha256_encrypted":data_sha or None,"age_encrypted":data_state=="present"}},
  sort_keys=True,separators=(",",":")))
PY
manifest_key="${key_base}.manifest.json"
aws_backup s3 cp "$tmp_dir/manifest.json" "s3://${bucket}/${manifest_key}" "${sse_args[@]}" --content-type application/json --only-show-errors

# Merge only this named lifecycle rule so existing bucket retention policies remain intact.
rule_id="savegeo-backup-retention-v1"
rules="$(aws_backup s3api get-bucket-lifecycle-configuration --bucket "$bucket" --output json 2>/dev/null | jq -c '.Rules // []' || printf '[]')"
merged="$(jq --arg id "$rule_id" --arg prefix "${object_prefix%/}/" --argjson days "$RETENTION_DAYS" \
  'map(select(.ID != $id)) + [{"ID":$id,"Filter":{"Prefix":$prefix},"Status":"Enabled","Expiration":{"Days":$days},"NoncurrentVersionExpiration":{"NoncurrentDays":$days},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]' <<< "$rules")"
aws_backup s3api put-bucket-lifecycle-configuration --bucket "$bucket" --lifecycle-configuration "$(jq -cn --argjson Rules "$merged" '{Rules:$Rules}')"
aws_backup s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled
printf 'Encrypted SAVEGEO backup uploaded: %s (database sha256=%s, evidence=%s, retention=%s days)\n' \
  "s3://${bucket}/${manifest_key}" "$db_sha" "$evidence_state" "$RETENTION_DAYS"
printf 'Persistent application data archive: %s\n' "$data_state"
