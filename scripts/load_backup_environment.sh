#!/usr/bin/env bash

# Read only backup-related assignments from the Compose .env file. Never
# source/eval that file: it is configuration data and must not execute shell.
backup_load_environment() {
  local env_file="${SAVEGEO_BACKUP_ENV_FILE:-.env}" assignment key encoded value
  [[ -f "$env_file" && ! -L "$env_file" ]] || return 0

  while IFS= read -r assignment; do
    key="${assignment%%=*}"
    encoded="${assignment#*=}"
    case "$key" in
      SAVEGEO_BACKUP_S3_URI|SAVEGEO_BACKUP_AGE_RECIPIENT|SAVEGEO_BACKUP_RETENTION_DAYS|\
      SAVEGEO_BACKUP_COMPOSE_FILE|SAVEGEO_BACKUP_EVIDENCE_DIR|SAVEGEO_BACKUP_DATA_PATHS|\
      SAVEGEO_BACKUP_AGE_IDENTITY_FILE|SAVEGEO_RESTORE_POSTGRES_IMAGE|SAVEGEO_LOCAL_BACKUP_DIR)
        ;;
      *) continue ;;
    esac
    [[ -n "${!key:-}" ]] && continue
    value="$(printf '%s' "$encoded" | base64 --decode)"
    printf -v "$key" '%s' "$value"
    export "$key"
  done < <(python3 - "$env_file" <<'PY'
from __future__ import annotations

import base64
import re
import shlex
import sys
from pathlib import Path

allowed = {
    "SAVEGEO_BACKUP_S3_URI",
    "SAVEGEO_BACKUP_AGE_RECIPIENT",
    "SAVEGEO_BACKUP_RETENTION_DAYS",
    "SAVEGEO_BACKUP_COMPOSE_FILE",
    "SAVEGEO_BACKUP_EVIDENCE_DIR",
    "SAVEGEO_BACKUP_DATA_PATHS",
    "SAVEGEO_BACKUP_AGE_IDENTITY_FILE",
    "SAVEGEO_RESTORE_POSTGRES_IMAGE",
    "SAVEGEO_LOCAL_BACKUP_DIR",
}
assignment = re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$")
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    match = assignment.match(line)
    if not match or match.group(1) not in allowed:
        continue
    raw = match.group(2).strip()
    if raw.startswith(("'", '"')):
        try:
            parts = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            continue
        if len(parts) != 1:
            continue
        value = parts[0]
    else:
        value = raw.split(" #", 1)[0].rstrip()
    print(f"{match.group(1)}={base64.b64encode(value.encode()).decode()}")
PY
)
}

backup_load_environment
