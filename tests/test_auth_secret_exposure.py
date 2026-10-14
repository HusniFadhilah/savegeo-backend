from pathlib import Path

import pytest

from scripts.check_auth_secret import scan_frontend


def test_signing_secret_exposure_detected_in_nested_public_assets(tmp_path: Path):
    secret = "private-signing-key-for-isolated-test-only"
    assets = tmp_path / "assets"
    assets.mkdir()
    (tmp_path / "index.html").write_text("<html></html>")
    (assets / "old-bundle.js").write_text(f'const leaked = "{secret}";')
    assert scan_frontend(secret, tmp_path) == (2, 1)
    assert scan_frontend("different-private-signing-key", tmp_path) == (2, 0)


def test_missing_or_empty_public_directory_fails_closed(tmp_path: Path):
    with pytest.raises(ValueError, match="must exist"):
        scan_frontend("secret", tmp_path / "absent")
    with pytest.raises(ValueError, match="No public frontend files"):
        scan_frontend("secret", tmp_path)
