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


def test_remote_restore_drill_transfers_and_cleans_up_verification_helper() -> None:
    workflow = (
        ROOT / ".github/workflows/restore-local-backup-drill-production.yml"
    ).read_text(encoding="utf-8")

    assert "scripts/select_verified_backup.py" in workflow
    assert "SAVEGEO_BACKUP_SELECTOR='$remote_selector'" in workflow
    assert r'rm -f -- \"$remote_script\" \"$remote_selector\"' in workflow
