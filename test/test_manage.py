import json
from pathlib import Path

import pytest

import manage
from manage import SetupError, load_setup


def _setup_document():
    return {
        "admin": {"email": "Admin@Example.com", "password": "ChangeMe123!"},
        "selected_devices": ["boondock_edge"],
        "wifi": {
            "ssid": "boondockedge",
            "password": "edge@123",
            "ip_address": "10.42.0.1",
        },
        "preferences": {
            "inbox_view": "continuous",
            "message_sorting": "newest",
        },
    }


def _write_setup(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "setup.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_load_setup_accepts_installer_options(tmp_path):
    setup = load_setup(_write_setup(tmp_path, _setup_document()))

    assert setup["admin"]["email"] == "admin@example.com"
    assert setup["selected_devices"] == ["boondock_edge"]


@pytest.mark.parametrize(
    ("section", "key", "value", "message"),
    [
        ("admin", "password", "short", "at least 8"),
        ("preferences", "inbox_view", "cards", "inbox_view"),
        ("preferences", "message_sorting", "random", "message_sorting"),
    ],
)
def test_load_setup_rejects_invalid_options(tmp_path, section, key, value, message):
    document = _setup_document()
    document[section][key] = value

    with pytest.raises(SetupError, match=message):
        load_setup(_write_setup(tmp_path, document))


def test_load_setup_rejects_unknown_device(tmp_path):
    document = _setup_document()
    document["selected_devices"] = ["boondock_edge", "unknown"]

    with pytest.raises(SetupError, match="Unsupported selected_devices: unknown"):
        load_setup(_write_setup(tmp_path, document))


def _initialize_and_capture_settings(monkeypatch, tmp_path, document):
    saved_settings = {}
    saved_users = {}
    autoconfigured = []

    class SettingsManager:
        def get_user(self, email):
            return None

        def save_user(self, email, user):
            saved_users[email] = user
            return True

        def set_all_settings(self, settings):
            saved_settings.update(settings)
            return True

        def get_all_settings(self):
            return {
                "ssid": "boondockedge",
                "host_password": "edge@123",
                "host_ip": "10.42.0.1",
                "host_port": "4000",
            }

        def save_pagination_prefs(self, email, preferences):
            return True

    monkeypatch.setattr(manage, "DATA_ROOT", tmp_path)
    monkeypatch.setattr("app.services.db_initializer.initialize_settings_database", lambda: True)
    monkeypatch.setattr("app.services.recordings_db_initializer.initialize_db", lambda: None)
    monkeypatch.setattr("app.services.settings_manager.get_settings_manager", SettingsManager)
    monkeypatch.setattr("app.utils.password_utils.hash_password", lambda password: "hashed")
    monkeypatch.setattr(
        manage,
        "auto_configure_connected_edge_devices",
        lambda settings_manager: autoconfigured.append(settings_manager),
    )

    manage.initialize(document)

    return saved_settings, saved_users, autoconfigured


def test_initialize_writes_installer_wifi_settings(monkeypatch, tmp_path):
    document = _setup_document()

    saved_settings, saved_users, autoconfigured = _initialize_and_capture_settings(
        monkeypatch, tmp_path, document
    )

    assert not (tmp_path / "db" / "admin.json").exists()
    assert saved_settings["ssid"] == "boondockedge"
    assert saved_settings["host_password"] == "edge@123"
    assert saved_settings["host_ip"] == "10.42.0.1"
    assert "hotspot_initial_setup_done" not in saved_settings
    assert saved_users["Admin@Example.com"]["preferences"]["inbox"]["records_per_page"] == 0
    assert len(autoconfigured) == 1


def test_initialize_skips_usb_autoconfig_without_boondock_edge(monkeypatch, tmp_path):
    document = _setup_document()
    document["selected_devices"] = ["usb_audio"]

    _, _, autoconfigured = _initialize_and_capture_settings(monkeypatch, tmp_path, document)

    assert autoconfigured == []


def test_auto_configures_all_connected_edge_usb_devices(monkeypatch):
    class Port:
        def __init__(self, device, description):
            self.device = device
            self.description = description
            self.manufacturer = None
            self.product = None
            self.vid = None
            self.pid = None

    ports = [
        Port("/dev/ttyUSB0", "CP2102 USB to UART Bridge Controller"),
        Port("/dev/ttyUSB1", "ESP32 USB to UART"),
        Port("/dev/ttyS0", "Built-in serial port"),
    ]
    calls = []

    class SettingsManager:
        def get_all_settings(self):
            return {
                "ssid": "boondockedge",
                "host_password": "edge@123",
                "host_ip": "10.42.0.1",
                "host_port": "4000",
            }

    monkeypatch.setattr("serial.tools.list_ports.comports", lambda: ports)
    monkeypatch.setattr(
        "app.services.recorder_monitor.run_autoconfig_sequence",
        lambda *args: calls.append(args) or {"success": True},
    )

    results = manage.auto_configure_connected_edge_devices(SettingsManager())

    assert list(results) == ["/dev/ttyUSB0", "/dev/ttyUSB1"]
    assert calls == [
        ("/dev/ttyUSB0", "boondockedge", "edge@123", "10.42.0.1", "4000"),
        ("/dev/ttyUSB1", "boondockedge", "edge@123", "10.42.0.1", "4000"),
    ]
