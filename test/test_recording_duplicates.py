"""Recording duplicate identity tests."""

from app.utils import crc_utils


def test_duplicate_identity_uses_filename_size_and_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(crc_utils, "DUPLICATE_CACHE_FILE", tmp_path / "duplicates.json")
    monkeypatch.setattr(
        crc_utils, "check_database_for_file", lambda *args: {"found": False}
    )

    first = crc_utils.check_and_update_duplicate_cache(b"abc", 1, "dispatch.wav")
    duplicate = crc_utils.check_and_update_duplicate_cache(b"xyz", 1, "dispatch.wav")
    different_size = crc_utils.check_and_update_duplicate_cache(
        b"abcd", 1, "dispatch.wav"
    )
    different_name = crc_utils.check_and_update_duplicate_cache(b"xyz", 1, "other.wav")
    different_channel = crc_utils.check_and_update_duplicate_cache(
        b"xyz", 2, "dispatch.wav"
    )

    assert first["is_duplicate"] is False
    assert duplicate["is_duplicate"] is True
    assert different_size["is_duplicate"] is False
    assert different_name["is_duplicate"] is False
    assert different_channel["is_duplicate"] is False
    assert duplicate["crc"] is None
