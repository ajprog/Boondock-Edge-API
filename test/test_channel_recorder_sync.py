import json
from unittest.mock import Mock


def test_import_maps_echo_tango_export_fields(monkeypatch):
    from app.services import channel_recorder_sync as sync

    saved = []
    monkeypatch.setattr(sync._settings_manager, "save_channel", lambda channel: saved.append(channel) or 7)

    channel = {"id": 7, "mac": "AABBCCDDEEFF"}
    updated = sync.import_recorder_configuration(
        channel,
        {"port": "/dev/ttyUSB0"},
        {
            "a": {
                "ath": 42,
                "stm": 1600,
                "mrm": 1200,
                "xrm": 25000,
                "cg": 4,
            },
            "ho": "edge-7.local",
            "cip": {"ip": "192.168.4.7"},
        },
    )

    assert updated["threshold"] == "42"
    assert updated["silence"] == "1600"
    assert updated["min_rec"] == "1200"
    assert updated["max_rec"] == "25000"
    assert updated["audio_gain"] == "4"
    assert updated["device_hostname"] == "edge-7.local"
    assert updated["device_ip"] == "192.168.4.7"
    assert saved == [updated]


def test_channel_export_imports_one_merged_echo_tango_document(monkeypatch):
    from app.services import channel_recorder_sync as sync

    commands = []
    port = "/dev/ttyUSB0"

    def confirm(_port, command, **kwargs):
        commands.append(command)
        assert kwargs == {"log_command": "IMPORT [configuration omitted]"}
        return True, {"status": "ok"}

    monkeypatch.setattr(sync, "send_command_and_wait_response", confirm)
    monkeypatch.setattr(sync._settings_manager, "save_channel", lambda channel: channel["id"])
    sync.forget_recorder_configuration(port)
    sync.import_recorder_configuration(
        {"id": 7, "mac": "AABBCCDDEEFF"},
        {"port": port},
        {
            "a": {"ath": 10, "stm": 1000, "mrm": 1000, "xrm": 5000, "cg": 2},
            "w": [{"ss": "field-network", "pw": "secret"}],
            "u": {"en": [True, False]},
        },
    )

    sync.export_channel_configuration(
        {
            "threshold": "42",
            "silence": "1600",
            "min_rec": "1200",
            "max_rec": "25000",
            "audio_gain": "4",
        },
        {"port": port},
    )

    assert len(commands) == 1
    command, payload = commands[0].split(" ", 1)
    assert command == "IMPORT"
    imported = json.loads(payload)
    assert imported["a"] == {
        "ath": 42,
        "stm": 1600,
        "mrm": 1200,
        "xrm": 25000,
        "cg": 4,
    }
    assert imported["w"] == [{"ss": "field-network", "pw": "secret"}]
    assert imported["u"] == {"en": [True, False]}


def test_channel_import_requires_initial_export(monkeypatch):
    from app.services import channel_recorder_sync as sync

    port = "/dev/ttyUSB9"
    sync.forget_recorder_configuration(port)
    monkeypatch.setattr(
        sync,
        "send_command_and_wait_response",
        lambda *_args: (_ for _ in ()).throw(AssertionError("device should not be called")),
    )

    try:
        sync.export_channel_configuration({"threshold": "42"}, {"port": port})
    except RuntimeError as error:
        assert str(error) == "Recorder configuration has not been exported"
    else:
        raise AssertionError("missing export should fail")


def test_monitor_requests_and_applies_one_initial_export(monkeypatch):
    from app.services import channel_recorder_sync, recorder_monitor

    port = "/dev/ttyUSB0"
    mac = "AABBCCDDEEFF"
    requested = []
    imported = []

    class Manager:
        @staticmethod
        def get_channel_by_mac(request_mac):
            assert request_mac == mac
            return {"id": 7, "mac": mac}

    monkeypatch.setattr(recorder_monitor, "_settings_manager", Manager())
    monkeypatch.setattr(recorder_monitor, "_ensure_channel_for_mac", lambda _mac: None)
    monkeypatch.setattr(
        recorder_monitor,
        "send_command_to_port",
        lambda requested_port, command: requested.append((requested_port, command)) or True,
    )
    monkeypatch.setattr(
        channel_recorder_sync,
        "import_recorder_configuration",
        lambda channel, recorder, config: imported.append((channel, recorder, config)),
    )
    recorder_monitor._pending_config_exports.clear()
    recorder_monitor._synchronized_config_mac.clear()
    recorder_monitor._last_channel_check_time.clear()
    recorder_monitor._last_known_mac.clear()

    recorder_monitor._add_message(port, json.dumps({"ty": "short", "mc": mac}))
    recorder_monitor._add_message(port, json.dumps({"audio": {"threshold": 42}}))
    recorder_monitor._add_message(port, json.dumps({"ty": "short", "mc": mac}))

    assert requested == [(port, "EXPORT")]
    assert imported == [(
        {"id": 7, "mac": mac},
        {"port": port},
        {"audio": {"threshold": 42}},
    )]
    assert port not in recorder_monitor._pending_config_exports


def test_serial_transport_uses_export_and_import_commands(monkeypatch):
    from app.services import recorder_config_transport as transport

    connections = []

    class Connection:
        def __init__(self, **_kwargs):
            self.writes = []
            self._reads = []
            self._lines = []
            connections.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def reset_input_buffer(self):
            pass

        def reset_output_buffer(self):
            pass

        def write(self, value):
            self.writes.append(value)

        def flush(self):
            pass

        def read(self, _size):
            return self._reads.pop(0) if self._reads else b''

        def readline(self):
            return self._lines.pop(0) if self._lines else b''

    def serial_factory(**kwargs):
        connection = Connection(**kwargs)
        if len(connections) == 1:
            connection._reads.append(b'{"a":{"ath":42}}\n')
        else:
            connection._lines.append(b'IMPORT OK\n')
        return connection

    monkeypatch.setattr(transport, "Serial", serial_factory)
    monkeypatch.setattr(transport.time, "sleep", Mock())

    assert transport.read_recorder_config("/dev/ttyUSB0") == {"a": {"ath": 42}}
    transport.write_recorder_config(
        "/dev/ttyUSB0", {"a": {"ath": 55}, "u": {"en": [True]}}
    )

    assert connections[0].writes == [b"EXPORT\n"]
    assert connections[1].writes == [
        b'IMPORT {"a":{"ath":55},"u":{"en":[true]}}\n'
    ]