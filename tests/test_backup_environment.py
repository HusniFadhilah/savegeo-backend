from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_backup_environment_reads_allowlisted_values_without_executing_env(tmp_path):
    bash = shutil.which("bash")
    python = shutil.which("python3")
    if not bash or not python:
        pytest.skip("The backup environment loader is exercised in the Linux CI runner.")

    marker = tmp_path / "shell-command-ran"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'SAVEGEO_BACKUP_S3_URI="s3://savegeo-test/private prefix"',
                'SAVEGEO_BACKUP_S3_ENDPOINT_URL="http://127.0.0.1:8333"',
                "SAVEGEO_BACKUP_S3_ACCESS_KEY_ID=test-access-key",
                "SAVEGEO_BACKUP_S3_SECRET_ACCESS_KEY='$(touch ignored-secret-marker)'",
                'SAVEGEO_BACKUP_AWS_CONFIG_FILE="/tmp/aws-config-test"',
                f"SAVEGEO_BACKUP_DATA_PATHS='var/saved_models $(touch {marker})'",
                "SAVEGEO_BACKUP_RETENTION_DAYS=90",
                "UNTRUSTED_VALUE=$(touch ignored-marker)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    helper = Path(__file__).parents[1] / "scripts" / "load_backup_environment.sh"
    process_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("SAVEGEO_BACKUP_") and key != "SAVEGEO_RESTORE_POSTGRES_IMAGE"
    }
    process_env.update(
        SAVEGEO_BACKUP_ENV_FILE=str(env_file),
        SAVEGEO_BACKUP_RETENTION_DAYS="30",
    )
    result = subprocess.run(
        [
            bash,
            "-c",
            'source "$1"; printf "%s\\n" "$SAVEGEO_BACKUP_S3_URI" "$SAVEGEO_BACKUP_DATA_PATHS" "$SAVEGEO_BACKUP_RETENTION_DAYS" "$SAVEGEO_BACKUP_S3_ENDPOINT_URL" "$SAVEGEO_BACKUP_S3_ACCESS_KEY_ID" "$SAVEGEO_BACKUP_S3_SECRET_ACCESS_KEY" "$SAVEGEO_BACKUP_AWS_CONFIG_FILE"',
            "backup-environment-test",
            str(helper),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=process_env,
    )

    assert result.stdout.splitlines() == [
        "s3://savegeo-test/private prefix",
        f"var/saved_models $(touch {marker})",
        "30",
        "http://127.0.0.1:8333",
        "test-access-key",
        "$(touch ignored-secret-marker)",
        "/tmp/aws-config-test",
    ]
    assert not marker.exists()
    assert not (tmp_path / "ignored-marker").exists()
    assert not (tmp_path / "ignored-secret-marker").exists()
