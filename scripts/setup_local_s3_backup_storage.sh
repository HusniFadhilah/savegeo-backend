#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
script_dir="$repo_root/scripts"
private_backup_root="$(cd "$repo_root/.." && pwd)/private-backups"
storage_root="$private_backup_root/local-s3"
secrets_dir="$storage_root/secrets"
data_dir="$storage_root/data"
s3_config="$storage_root/s3-config.json"
aws_config="$storage_root/aws-cli-config"
age_identity="$secrets_dir/age-identity.txt"
env_file="$repo_root/.env.backup"

for command in age age-keygen aws docker jq python3; do
  command -v "$command" >/dev/null 2>&1 || { echo "Required setup command is missing: $command" >&2; exit 2; }
done

mkdir -p "$secrets_dir" "$data_dir"
chmod 700 "$storage_root" "$secrets_dir" "$data_dir"

if [[ ! -e "$env_file" ]]; then
  [[ ! -L "$env_file" ]] || { echo "Refusing to write through a symlink: $env_file" >&2; exit 2; }
  access_key="savegeo$(python3 -c 'import secrets; print(secrets.token_hex(12))')"
  secret_key="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  if [[ ! -e "$age_identity" ]]; then
    age-keygen -o "$age_identity" >/dev/null 2>&1
  fi
  chmod 600 "$age_identity"
  recipient="$(age-keygen -y "$age_identity")"
  cat > "$env_file" <<EOF
SAVEGEO_BACKUP_S3_URI="s3://savegeo-backups/production"
SAVEGEO_BACKUP_S3_ENDPOINT_URL="http://127.0.0.1:8333"
SAVEGEO_BACKUP_S3_ACCESS_KEY_ID="$access_key"
SAVEGEO_BACKUP_S3_SECRET_ACCESS_KEY="$secret_key"
SAVEGEO_BACKUP_AWS_CONFIG_FILE="$aws_config"
SAVEGEO_BACKUP_AGE_RECIPIENT="$recipient"
SAVEGEO_BACKUP_AGE_IDENTITY_FILE="$age_identity"
SAVEGEO_BACKUP_RETENTION_DAYS=30
EOF
  chmod 600 "$env_file"
fi

[[ -f "$env_file" && ! -L "$env_file" ]] || { echo "Backup environment file is missing or unsafe." >&2; exit 2; }
SAVEGEO_BACKUP_ENV_FILE="$env_file"
source "$script_dir/load_backup_environment.sh"
: "${SAVEGEO_BACKUP_S3_ENDPOINT_URL:?Missing local S3 endpoint in .env.backup}"
: "${SAVEGEO_BACKUP_S3_ACCESS_KEY_ID:?Missing local S3 access key in .env.backup}"
: "${SAVEGEO_BACKUP_S3_SECRET_ACCESS_KEY:?Missing local S3 secret key in .env.backup}"
: "${SAVEGEO_BACKUP_AGE_RECIPIENT:?Missing age recipient in .env.backup}"
: "${SAVEGEO_BACKUP_AGE_IDENTITY_FILE:?Missing age identity path in .env.backup}"

[[ -f "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE" && ! -L "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE" ]] || {
  echo "Configured age identity file is missing or unsafe." >&2
  exit 2
}
[[ "$(age-keygen -y "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE")" == "$SAVEGEO_BACKUP_AGE_RECIPIENT" ]] || {
  echo "The age identity does not match the configured recipient." >&2
  exit 2
}
[[ "$(stat -c '%a' "$SAVEGEO_BACKUP_AGE_IDENTITY_FILE")" == 600 ]] || { echo "Age identity must have mode 600." >&2; exit 2; }

export SAVEGEO_BACKUP_S3_ACCESS_KEY_ID SAVEGEO_BACKUP_S3_SECRET_ACCESS_KEY
python3 - "$s3_config" <<'PY'
import json
import os
import sys
from pathlib import Path

target = Path(sys.argv[1])
temporary = target.with_suffix(".json.tmp")
configuration = {
    "identities": [{
        "name": "savegeo-backup",
        "credentials": [{
            "accessKey": os.environ["SAVEGEO_BACKUP_S3_ACCESS_KEY_ID"],
            "secretKey": os.environ["SAVEGEO_BACKUP_S3_SECRET_ACCESS_KEY"],
        }],
        "actions": ["Admin", "Read", "Write"],
    }]
}
temporary.write_text(json.dumps(configuration, separators=(",", ":")) + "\n", encoding="utf-8")
temporary.chmod(0o600)
temporary.replace(target)
PY

cat > "$aws_config" <<'EOF'
[default]
region = us-east-1
s3 =
  addressing_style = path
EOF
chmod 600 "$aws_config" "$s3_config"

docker compose --env-file "$env_file" -f "$repo_root/docker-compose.backup-storage.yml" up -d

export AWS_ACCESS_KEY_ID="$SAVEGEO_BACKUP_S3_ACCESS_KEY_ID"
export AWS_SECRET_ACCESS_KEY="$SAVEGEO_BACKUP_S3_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION=us-east-1
export AWS_EC2_METADATA_DISABLED=true
export AWS_CONFIG_FILE="$aws_config"
bucket_path="${SAVEGEO_BACKUP_S3_URI#s3://}"
bucket="${bucket_path%%/*}"
object_prefix="${bucket_path#*/}"
aws_backup() { aws --endpoint-url "$SAVEGEO_BACKUP_S3_ENDPOINT_URL" "$@"; }

ready=0
for attempt in $(seq 1 60); do
  if aws_backup s3api list-buckets --output json >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
(( ready == 1 )) || { echo "Local S3 endpoint did not become ready." >&2; exit 3; }

if ! aws_backup s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
  aws_backup s3api create-bucket --bucket "$bucket" --region us-east-1
fi
aws_backup s3api put-bucket-versioning --bucket "$bucket" --versioning-configuration Status=Enabled

rules="$(aws_backup s3api get-bucket-lifecycle-configuration --bucket "$bucket" --output json 2>/dev/null | jq -c '.Rules // []' || printf '[]')"
merged="$(jq --arg id "savegeo-backup-retention-v1" --arg prefix "${object_prefix%/}/" --argjson days "${SAVEGEO_BACKUP_RETENTION_DAYS:-30}" \
  'map(select(.ID != $id)) + [{"ID":$id,"Filter":{"Prefix":$prefix},"Status":"Enabled","Expiration":{"Days":$days},"NoncurrentVersionExpiration":{"NoncurrentDays":$days},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]' <<< "$rules")"
aws_backup s3api put-bucket-lifecycle-configuration --bucket "$bucket" --lifecycle-configuration "$(jq -cn --argjson Rules "$merged" '{Rules:$Rules}')"

probe_dir="$(mktemp -d "${TMPDIR:-/tmp}/savegeo-s3-probe.XXXXXX")"
trap 'rm -rf -- "$probe_dir"' EXIT
probe_key="${object_prefix%/}/.setup-check/$(date -u +%Y%m%dT%H%M%SZ).txt"
printf 'SAVEGEO local S3 endpoint check\n' > "$probe_dir/check.txt"
aws_backup s3 cp "$probe_dir/check.txt" "s3://${bucket}/${probe_key}" --only-show-errors
aws_backup s3 cp "s3://${bucket}/${probe_key}" "$probe_dir/check-download.txt" --only-show-errors
cmp -s "$probe_dir/check.txt" "$probe_dir/check-download.txt" || { echo "Local S3 object read-back check failed." >&2; exit 4; }
aws_backup s3api delete-object --bucket "$bucket" --key "$probe_key" >/dev/null

printf 'Local S3 storage is ready at %s (bucket=%s, retention=%s days).\n' \
  "$SAVEGEO_BACKUP_S3_ENDPOINT_URL" "$bucket" "${SAVEGEO_BACKUP_RETENTION_DAYS:-30}"
