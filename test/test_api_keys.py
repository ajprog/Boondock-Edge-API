"""API-key principal and credential lifecycle tests."""

from app.services.api_key_manager import APIKeyManager


def test_api_key_issuer_writes_principal_and_shared_credential(
    initialized_settings_manager,
):
    repository = initialized_settings_manager
    repository.save_user(
        "admin@example.com",
        {
            "name": "Admin",
            "password": "unused",
            "role": "admin",
            "status": "Active",
            "groups": [],
        },
    )
    manager = APIKeyManager(db_path=repository.db_path)
    manager.settings = repository

    metadata, token = manager.create_key(
        "Integration", scopes=["recording.create"], created_by="admin@example.com"
    )

    assert token.startswith("bk_live_")
    assert metadata["permissions"] == ["recording.create"]
    assert metadata["owner"] == "user:admin@example.com"
    credential = repository.get_credential(token)
    assert credential["principal_type"] == "api_key"
    assert credential["principal_id"] == metadata["id"]
    assert manager.revoke_key(metadata["id"]) is True
    assert repository.get_credential(token) is None
