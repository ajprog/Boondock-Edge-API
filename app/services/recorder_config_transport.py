"""Low-level USB recorder configuration transport."""

import json
import time

from serial import Serial, SerialException, SerialTimeoutException


def read_recorder_config(port, timeout_seconds=8.0):
    """Read and decode the recorder's exported configuration."""
    try:
        with Serial(port=port, baudrate=115200, timeout=0.2, write_timeout=1) as connection:
            try:
                connection.reset_input_buffer()
                connection.reset_output_buffer()
            except (SerialException, AttributeError):
                pass

            # Echo/Tango's serial CLI exports the saved settings as one JSON
            # document in response to EXPORT.
            connection.write(b'EXPORT\n')
            connection.flush()
            deadline = time.time() + timeout_seconds
            buffer = bytearray()
            while time.time() < deadline:
                chunk = connection.read(4096)
                if not chunk:
                    time.sleep(0.05)
                    continue
                buffer.extend(chunk)
                text = buffer.decode('utf-8', errors='ignore')
                start = text.find('{')
                end = text.rfind('}')
                if start < 0 or end <= start:
                    continue
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    continue
            raise TimeoutError('Recorder did not return configuration before timeout.')
    except SerialTimeoutException as exc:
        raise TimeoutError('Timed out waiting for configuration response.') from exc


def write_recorder_config(port, config):
    """Import one complete Echo/Tango configuration document."""
    payload = json.dumps(config, separators=(',', ':'), ensure_ascii=True)
    with Serial(port=port, baudrate=115200, timeout=1, write_timeout=1) as connection:
        try:
            connection.reset_input_buffer()
            connection.reset_output_buffer()
        except (SerialException, AttributeError):
            pass

        connection.write(f'IMPORT {payload}\n'.encode('utf-8'))
        connection.flush()

        deadline = time.time() + 5.0
        confirmed = False
        while time.time() < deadline:
            line = connection.readline()
            if not line:
                continue
            response = line.decode('utf-8', errors='replace').strip()
            try:
                document = json.loads(response[response.find('{'):response.rfind('}') + 1])
            except (json.JSONDecodeError, ValueError):
                document = None
            if (isinstance(document, dict) and document.get('status') == 'ok') or any(
                marker in response.upper()
                for marker in ('CONFIG OK', 'IMPORT OK', 'SAVE OK', 'WRITE OK')
            ):
                confirmed = True
                break
        if not confirmed:
            raise TimeoutError('Recorder did not confirm configuration import')