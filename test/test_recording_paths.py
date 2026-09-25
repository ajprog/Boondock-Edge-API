from datetime import datetime, timezone

from app.routes import route_utils


def test_get_recording_path_preserves_uploaded_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(route_utils, "DATA_ROOT", tmp_path)
    recorded_at = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)

    absolute_path = route_utils.get_recording_path(
        "AABBCCDDEEFF", recorded_at, "device-recording.wav"
    )

    assert (
        absolute_path
        == tmp_path / "recordings/aabbccddeeff/2026/08/28/device-recording.wav"
    )


def test_get_recording_path_appends_microseconds_for_duplicate(tmp_path, monkeypatch):
    monkeypatch.setattr(route_utils, "DATA_ROOT", tmp_path)
    recorded_at = datetime(2026, 8, 28, 12, 34, 56, tzinfo=timezone.utc)
    original = (
        tmp_path
        / "recordings"
        / "aabbccddeeff"
        / "2026"
        / "08"
        / "28"
        / "device-recording.wav"
    )
    original.parent.mkdir(parents=True)
    original.write_bytes(b"existing")

    absolute_path = route_utils.get_recording_path(
        "AABBCCDDEEFF", recorded_at, "device-recording.wav"
    )

    stem, suffix = absolute_path.name.rsplit("_", 1)
    assert stem == "device-recording"
    assert suffix.endswith(".wav")
    assert len(suffix.removesuffix(".wav")) == 6
    assert suffix.removesuffix(".wav").isdigit()
