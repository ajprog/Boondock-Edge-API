# Boondock Edge API

The local API and device-services backend for **Boondock Edge**. This Flask and
Socket.IO application receives radio/device events and audio, manages channels
and recordings, provides optional local transcription and backup services, and
serves the bundled dashboard documentation.

> [!IMPORTANT]
> This software is a supplemental informational or hobby tool. It is **not** a
> PSAP, 911 service, emergency dispatch or notification system, or life-safety
> system. Do not rely on it to protect life or property. Read
> [SAFETY.md](SAFETY.md) before installing or using it.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Initial setup](#initial-setup)
- [Running and configuration](#running-and-configuration)
- [Using the API](#using-the-api)
- [Data and backups](#data-and-backups)
- [Troubleshooting](#troubleshooting)
- [Development and testing](#development-and-testing)
- [License](#license)

## Features

- Local REST API and Socket.IO server for the Boondock Edge dashboard.
- Support for Boondock Edge recorders, compatible cloud-style devices, Uniden
  scanners, USB audio devices, and optional GPIO controls.
- Recording ingestion, channel management, history, tags, incident reports,
  health metrics, and device logs.
- Optional local speech-to-text using `faster-whisper`.
- Optional S3 and network-drive backup workflows.
- Swagger UI and bundled operator documentation.

Device firmware compatibility and supported device endpoints are documented in
[COMPATIBILITY.md](COMPATIBILITY.md).

## Requirements

- Python 3 with `venv` and `pip` (a currently supported Python release is
  recommended).
- A writable directory for SQLite databases, recordings, and logs.
- For hardware features: compatible USB/serial/audio hardware and permission to
  access it. Linux users may need membership in groups such as `dialout` and
  `audio`.
- Platform libraries required by optional packages such as PortAudio, FFmpeg,
  and the selected Whisper backend.

The server can run on Linux or Windows. Linux is recommended for an appliance
deployment and is required for systemd-based service management.

## Quick start

```bash
git clone https://github.com/Boondock-Echo/Boondock-Edge-API.git
cd Boondock-Edge-API

python3 -m venv .venv
source .venv/bin/activate             # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create the initial administrator and application settings as described below,
then start the API:

```bash
python manage.py setup --config setup.json
python run.py
```

The default address is <http://localhost:4000>. Stop the foreground server with
<kbd>Ctrl</kbd>+<kbd>C</kbd>.

## Initial setup

`manage.py setup` validates a JSON setup document, creates the SQLite
databases, saves an administrator account, and applies the selected device and
inbox preferences. Create `setup.json` in the repository root:

```json
{
  "admin": {
    "email": "admin@example.com",
    "password": "replace-with-a-long-unique-password"
  },
  "selected_devices": [
    "boondock_edge",
    "uniden_scanner"
  ],
  "wifi": {
    "ssid": "your-network-name",
    "password": "your-network-password",
    "ip_address": "192.168.1.50"
  },
  "preferences": {
    "inbox_view": "continuous",
    "message_sorting": "newest"
  }
}
```

Valid `selected_devices` entries are `boondock_edge`, `uniden_scanner`,
`usb_audio`, and `gpio`. The inbox view may be `continuous` or `pagination`,
and message sorting may be `newest` or `oldest`. Provide the `wifi` object even
when no Edge recorder is selected; these host settings are saved for later use.

Run setup with:

```bash
python manage.py setup --config setup.json
```

Setup is designed to be idempotent, but running it again updates the named
administrator and installation preferences. If a Boondock Edge USB device is
selected and connected, setup also attempts to configure the compatible serial
device with the saved host network settings.

Setup also imports the bundled phrases in `app/services/hallucinations.txt`
into the hallucination-pattern database. These defaults match the whole
transcript, ignoring case and surrounding whitespace, so words such as `you`
do not flag otherwise valid sentences. Repeated setup skips defaults already
present and preserves custom patterns. This import does not reclassify existing
recordings; classification occurs when transcription text is saved.

> [!CAUTION]
> `setup.json` contains credentials. Do not commit it, share it, or leave it
> readable by other users. Delete it or move it to a protected location after
> setup.

## Running and configuration

### Development mode

Development mode enables Flask debugging and automatic reload:

```bash
PRODUCTION_MODE=false python run.py
```

On Windows Command Prompt:

```bat
set PRODUCTION_MODE=false && python run.py
```

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `FLASK_PORT` | `4000` | TCP port on which the API listens. |
| `PRODUCTION_MODE` | `true` | Use production behavior; set to `false` for debug/reload. |
| `PRODUCTION_SERVER` | `gevent` | Requested production server mode. |
| `SOCKETIO_ASYNC_MODE` | `auto` | Set to `threading` to bypass an incompatible gevent installation. |
| `BOONDOCK_DATA_ROOT` | Parent of the repository | Root for persistent databases, recordings, logs, dashboard assets, and device settings. |
| `SECRET_KEY` | Generated and persisted | Stable Flask signing secret; set an explicit high-entropy value in production. |
| `CORS_ALLOWED_ORIGINS` | `*` | Comma-separated trusted dashboard origins. Do not leave unrestricted on an exposed deployment. |

For example:

```bash
export BOONDOCK_DATA_ROOT=/var/lib/boondock-edge
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export CORS_ALLOWED_ORIGINS=https://edge.example.com
python run.py
```

The application binds to all interfaces (`0.0.0.0`). Use a host firewall and a
TLS-terminating reverse proxy when making it available outside a trusted LAN.
Do not expose device, administration, Swagger, or recording endpoints directly
to the public internet.

## Using the API

After startup, the following entry points are useful:

| URL | Description |
| --- | --- |
| `/` | Dashboard, when dashboard assets exist under the data root. |
| `/api-docs` | Interactive Swagger API reference. |
| `/apispec.json` | Machine-readable Swagger specification. |
| `/docs/` | Bundled Boondock Edge documentation. |
| `/api/settings/ping` | Simple API connectivity check. |
| `/api/health/system?current=true` | Current system health metrics. |

Most administration endpoints use a bearer session token obtained through the
authentication API. External integrations can use scoped API keys created by
an administrator. Send either credential in the standard header:

```text
Authorization: Bearer <token-or-api-key>
```

Consult Swagger for current request and response schemas. For device-facing
firmware, event, audio, and log endpoint behavior, see
[COMPATIBILITY.md](COMPATIBILITY.md).

## Data and backups

By default, persistent data is written to the parent directory of this checkout.
Set `BOONDOCK_DATA_ROOT` explicitly so upgrades and deployments do not move or
overwrite operational data. The data root may contain:

```text
db/                 SQLite settings, recordings metadata, logs, and secret key
recordings/         Received and processed audio
logs/               Application and device logs
device_settings/    Per-device configuration
dashboard/          Optional dashboard build
docs/               Downloadable documentation files
```

Back up the entire data root while writes are stopped, or use the application's
configured S3/network-drive backup features. Protect backups as sensitive data:
they may contain credentials, operational configuration, logs, transcriptions,
and recorded audio. Test restores regularly; a successful upload alone is not a
verified backup.

## Troubleshooting

### The server does not start

1. Activate the intended virtual environment and reinstall dependencies:
   `python -m pip install -r requirements.txt`.
2. Confirm the configured port is free. Change it with, for example,
   `FLASK_PORT=4001 python run.py`.
3. Confirm the user can create and write to `BOONDOCK_DATA_ROOT`, especially its
   `db`, `logs`, and `recordings` directories.
4. Read the final exception in the console output rather than only the first
   warning; optional hardware services may warn without stopping the API.

### `Illegal instruction`, gevent, or WebSocket startup errors

Some older CPUs cannot execute instructions used by a prebuilt gevent/greenlet
wheel. The application probes gevent before using it and normally falls back to
threading. Force that safe fallback when diagnosing the issue:

```bash
SOCKETIO_ASYNC_MODE=threading python run.py
```

### USB, scanner, or serial device is missing

- Check the cable, power, and device enumeration (`python -m serial.tools.list_ports`).
- On Linux, verify group membership and permissions for `/dev/ttyUSB*` or
  `/dev/ttyACM*`, then sign out and back in after changing groups.
- Stop other programs that may have opened the serial port.
- Re-run initial setup with the correct item in `selected_devices`.

### Audio capture or transcription fails

- Verify the audio device appears to the operating system and is not exclusively
  held by another process.
- Install the platform's PortAudio and FFmpeg runtime packages when required.
- Confirm there is enough free disk space and memory. Whisper model downloads
  and inference can be large and slow, especially without supported acceleration.
- Review application logs and the saved transcription settings before retrying.

### Dashboard is blank or returns 404

The API checkout includes the backend and documentation, but the dashboard is
served from `dashboard/` under `BOONDOCK_DATA_ROOT`. Ensure a compatible
dashboard build is installed there. The Swagger UI at `/api-docs` remains the
best way to validate the backend independently.

### Browser requests are blocked by CORS

Set `CORS_ALLOWED_ORIGINS` to the exact scheme and host used by the dashboard.
Multiple origins are comma-separated. Restart the API after changing it:

```bash
CORS_ALLOWED_ORIGINS=https://edge.example.com,http://192.168.1.50:3000 python run.py
```

### Authentication stops working after restart

Set a stable `SECRET_KEY` and ensure the process can read its data root. Without
an explicit key, the application persists one at `db/.secret_key`; deleting or
changing that file invalidates existing sessions.

## Development and testing

Install the dependencies, then run the regression suite from the repository
root:

```bash
python -m pytest -q
```

Keep changes focused and add tests for behavior changes. Contributions are not
currently accepted until the project's contributor agreement process is active;
read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Release
history is maintained in [RELEASE_NOTES.md](RELEASE_NOTES.md).

## License

Copyright © 2026 Boondock Technologies, LLC. All rights reserved.

This repository is **source-available, not open source** as that term is defined
by the Open Source Initiative. Personal and non-commercial educational use is
available only under the conditions in [LICENSE.md](LICENSE.md). Commercial use
requires a separate written commercial license; see
[COMMERCIAL-LICENSE.md](COMMERCIAL-LICENSE.md).

The license prohibits use as or within emergency dispatch, 911/PSAP,
life-safety, or property-safety systems. Distribution requires attribution,
preservation of legal notices, inclusion of the license and `NOTICE`, and the
other conditions stated in the license. This summary is not a substitute for
the license; if it conflicts with `LICENSE.md`, the license controls.

Commercial licensing and permission requests:

- Website: <https://boondockecho.com>
- Email: riki@boondocktechnologies.com
