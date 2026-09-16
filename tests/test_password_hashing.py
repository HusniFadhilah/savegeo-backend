from __future__ import annotations

import bcrypt

from app.core.security import hash_password, verify_password


def test_new_password_hash_is_scrypt_and_supports_long_passwords():
    password = "x" * 100 + "-correct"
    hashed = hash_password(password)
    assert hashed.startswith("scrypt$v1$")
    assert verify_password(password, hashed)
    assert not verify_password(password + "!", hashed)


def test_legacy_bcrypt_hash_remains_compatible():
    password = "legacy-password"
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    assert verify_password(password, hashed)
    assert not verify_password("wrong", hashed)
