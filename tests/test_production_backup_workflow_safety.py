from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_deploy_archives_private_dmrv_evidence_in_read_only_isolated_container() -> None:
    workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text(
        encoding="utf-8"
    )

    assert "docker run --rm --network none --read-only --user 0:0" in workflow
    assert "--cap-drop ALL --cap-add DAC_READ_SEARCH" in workflow
    assert '--volume "$BACKEND_REMOTE_DIR/var/dmrv:/source/dmrv:ro"' in workflow
    assert "postgres:16-alpine tar -C /source -czf - dmrv/evidence > \"$evidence_tmp\"" in workflow


def test_migration_inventory_container_can_write_private_deploy_owned_audit_files() -> None:
    workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text(
        encoding="utf-8"
    )

    assert 'audit_uid="$(id -u)"' in workflow
    assert 'audit_gid="$(id -g)"' in workflow
    assert workflow.count('--user "$audit_uid:$audit_gid"') == 3


def test_restore_drills_use_actual_restore_to_validate_stdin_archive_streams() -> None:
    local_script = (ROOT / "scripts/restore_local_backup_drill.sh").read_text(
        encoding="utf-8"
    )
    offsite_script = (ROOT / "scripts/restore_drill.sh").read_text(encoding="utf-8")
    for script in (local_script, offsite_script):
        assert "pg_restore --list -" not in script
        assert "pg_restore -U postgres -d savegeo_restore_check" in script
    assert '--no-owner --no-privileges < "$backup_path"' in local_script
    assert '--no-owner --no-privileges - < "$backup_path"' not in local_script


def test_remote_restore_drill_transfers_and_cleans_up_verification_helper() -> None:
    workflow = (
        ROOT / ".github/workflows/restore-local-backup-drill-production.yml"
    ).read_text(encoding="utf-8")

    assert "scripts/select_verified_backup.py" in workflow
    assert "SAVEGEO_BACKUP_SELECTOR='$remote_selector'" in workflow
    assert r'rm -f -- \"$remote_script\" \"$remote_selector\"' in workflow


def test_local_s3_backup_workflows_load_separate_secrets_and_run_weekly() -> None:
    backup = (ROOT / ".github/workflows/backup-production.yml").read_text(
        encoding="utf-8"
    )
    restore = (ROOT / ".github/workflows/restore-drill-production.yml").read_text(
        encoding="utf-8"
    )

    assert 'cron: "17 2 * * 0"' in backup
    assert "SAVEGEO_BACKUP_ENV_FILE=.env.backup bash scripts/backup_offsite.sh" in backup
    assert "SAVEGEO_BACKUP_ENV_FILE=.env.backup bash scripts/restore_drill.sh" in restore


def test_production_deploy_preserves_and_provisions_local_backup_storage() -> None:
    workflow = (ROOT / ".github/workflows/deploy-production.yml").read_text(
        encoding="utf-8"
    )

    assert "--exclude '.env.backup'" in workflow
    assert "- name: Provision encrypted local backup storage" in workflow
    assert "bash scripts/setup_local_s3_backup_storage.sh" in workflow


def test_offsite_backup_reads_private_evidence_in_isolated_read_only_container() -> None:
    script = (ROOT / "scripts/backup_offsite.sh").read_text(encoding="utf-8")

    assert '[[ -d "$EVIDENCE_DIR" && ! -L "$EVIDENCE_DIR" ]]' in script
    assert "docker run --rm --network none --read-only --user 0:0" in script
    assert "--cap-drop ALL --cap-add DAC_READ_SEARCH" in script
    assert '"$evidence_parent:/source:ro"' in script
    assert "postgres:16-alpine tar -C /source -czf - \"$evidence_name\"" in script


def test_offsite_backup_reads_private_runtime_data_in_isolated_read_only_container() -> None:
    script = (ROOT / "scripts/backup_offsite.sh").read_text(encoding="utf-8")

    assert '"$path" == var/*' in script
    assert '[[ ! -L "$path" ]]' in script
    assert "--volume \"$PWD/var:/source/var:ro\"" in script
    assert "postgres:16-alpine tar -C /source -czf - \"${archive_paths[@]}\"" in script
