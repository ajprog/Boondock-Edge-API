"""
Hotspot management routes.
Handles Wi-Fi hotspot start, stop, and status operations.
"""
import json
import logging
from flask import Blueprint, jsonify, request
from ..middleware.auth_middleware import require_admin

from app.services.hotspot_service import (
    get_hotspot_status,
    start_hotspot,
    stop_hotspot,
)
from ..routes.route_utils import init_settings
from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

hotspot_bp = Blueprint('hotspot', __name__)


@hotspot_bp.route('/hotspot/status', methods=['GET'])
@require_admin
def hotspot_status():
    """
    Return the current hotspot status and basic configuration summary.
    """
    try:
        # Ensure settings exist so host_* fields are present
        init_settings()
        # Detect and persist from one NetworkManager snapshot. Re-querying
        # here could return a different connection during a network handoff.
        status = get_hotspot_status()
        settings_sync = status.get("settings_sync")
        if settings_sync and not settings_sync.get("success"):
            failed = ", ".join(settings_sync.get("failed", []))
            raise RuntimeError(f"Failed to update external Wi-Fi settings: {failed}")

        settings = _settings_manager.get_all_settings()
        # Attach host mapping info used by recorder autoconfiguration
        status["host_settings"] = {
            "ssid": settings.get("ssid", ""),
            "host_ip": settings.get("host_ip", ""),
            "host_port": settings.get("host_port", "4000"),
        }
        return jsonify(status)
    except Exception as e:
        logging.error("Failed to get hotspot status: %s", e)
        return jsonify({"error": str(e)}), 500


@hotspot_bp.route('/hotspot/start', methods=['POST'])
@require_admin
def hotspot_start():
    """
    Create/update and start the hotspot on the edge device (Pi or mini PC).

    If no SSID/password are provided in the request body, the values from
    host_ssid and host_password in settings.json will be used.
    """
    try:
        init_settings()
        settings = _settings_manager.get_all_settings()

        payload = request.get_json(silent=True) or {}
        ssid = (payload.get("ssid") or settings.get("ssid") or "").strip()
        password = (payload.get("password") or settings.get("host_password") or "").strip()

        if not ssid or not password:
            return (
                jsonify(
                    {
                        "error": "Hotspot SSID and password are required. "
                                 "Configure them in Global Settings first."
                    }
                ),
                400,
            )

        if settings.get("external_wifi"):
            return (
                jsonify(
                    {
                        "error": "Hotspot can't be started, already connected to WiFi."
                    }
                ),
                400,
            )

        result = start_hotspot(ssid=ssid, password=password)
        status_code = 200 if result.get("success") else 500
        return jsonify(result), status_code
    except Exception as e:
        logging.error("Failed to start hotspot: %s", e)
        return jsonify({"error": str(e)}), 500


@hotspot_bp.route('/hotspot/stop', methods=['POST'])
@require_admin
def hotspot_stop():
    """
    Stop the hotspot connection if it is active.
    """
    try:
        result = stop_hotspot()
        status_code = 200 if result.get("success") else 500
        return jsonify(result), status_code
    except Exception as e:
        logging.error("Failed to stop hotspot: %s", e)
        return jsonify({"error": str(e)}), 500
