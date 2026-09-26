from __future__ import annotations

import pytest

from scripts import ensure_test_accounts


def test_test_accounts_have_expected_role_mapping():
    mapping = {account.username: account.role_name for account in ensure_test_accounts.TEST_ACCOUNTS}

    assert mapping == {
        "SavegeoGeospatial": "geospatial_expert",
        "example_user": "viewer",
        "demo_admin": "admin",
        "super_admin": None,
    }


def test_passwords_require_all_environment_values(monkeypatch):
    for account in ensure_test_accounts.TEST_ACCOUNTS:
        monkeypatch.delenv(account.password_env, raising=False)

    with pytest.raises(SystemExit):
        ensure_test_accounts._passwords_from_environment()
