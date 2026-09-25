"""Credential repository persistence and expiry tests."""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone


def test_credential_repository_hashes_and_resolves_credentials(
    initialized_settings_manager,
):
    repository = initialized_settings_manager
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    raw_token, credential_id = repository.issue_credential(
        "user", "member@example.com", expires_at
    )
    credential = repository.get_credential(raw_token)

    assert credential["id"] == credential_id
    assert credential["principal_type"] == "user"
    assert credential["principal_id"] == "member@example.com"
    connection = sqlite3.connect(repository.db_path)
    persisted = connection.execute(
        "SELECT token_hash FROM credentials WHERE id=?", (credential_id,)
    ).fetchone()[0]
    connection.close()
    assert persisted == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert persisted != raw_token


def test_credential_repository_reports_missing_and_expired(
    initialized_settings_manager,
):
    repository = initialized_settings_manager
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    expired_token, _ = repository.issue_credential("device", "1", expired_at)

    assert repository.inspect_credential("") == (None, "missing")
    assert repository.inspect_credential("unknown") == (None, "not_found")
    assert repository.inspect_credential(expired_token) == (None, "expired")
    assert repository.delete_expired_credentials() == 1
