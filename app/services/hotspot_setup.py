"""
First-run / install hotspot provisioning.

If the device supports AP mode, ensures default host_ssid/host_password are stored
and starts the BoondockHotspot connection.
"""

import logging
from typing import Any, Dict

from app.services.hotspot_service import (
    get_hotspot_status,
    get_wifi_interface,
    start_hotspot,
    _wifi_ap_supported,
)
from app.services.settings_manager import get_settings_manager

logger = logging.getLogger(__name__)

HOTSPOT_SETUP_FLAG = "hotspot_initial_setup_done"
DEFAULT_HOTSPOT_SSID = "boondockedge"
DEFAULT_HOTSPOT_PASSWORD = "edge@123"
DEFAULT_HOST_IP = "10.42.0.1"
DEFAULT_HOST_PORT = "4000"


def _ensure_host_settings(settings_manager) -> Dict[str, str]:
    """Insert default host_* settings only when missing or empty."""
    settings = settings_manager.get_all_settings() or {}
    ssid = (settings.get("ssid") or "").strip() or DEFAULT_HOTSPOT_SSID
    password = (settings.get("host_password") or "").strip() or DEFAULT_HOTSPOT_PASSWORD
    host_ip = (settings.get("host_ip") or "").strip() or DEFAULT_HOST_IP
    host_port = (settings.get("host_port") or "").strip() or DEFAULT_HOST_PORT

    if not (settings.get("ssid") or "").strip():
        settings_manager.set_setting("ssid", ssid)
    if not (settings.get("host_password") or "").strip():
        settings_manager.set_setting("host_password", password)
    if not (settings.get("host_ip") or "").strip():
        settings_manager.set_setting("host_ip", host_ip)
    if not (settings.get("host_port") or "").strip():
        settings_manager.set_setting("host_port", host_port)

    return {
        "host_ssid": ssid,
        "host_password": password,
        "host_ip": host_ip,
        "host_port": host_port,
    }


def run_initial_hotspot_setup(force: bool = False) -> Dict[str, Any]:
    """
    Configure and enable hotspot when hardware supports it.

    Idempotent unless force=True. Sets hotspot_initial_setup_done when complete
    or when AP mode is unavailable (ap_not_supported).
    """
    settings_manager = get_settings_manager()
    prior = settings_manager.get_all_settings()
    wifi_status = get_hotspot_status()
    if wifi_status.get("external_wifi"):
        return {
            "skipped": True,
            "reason": "external_wifi",
            "flag": "external_wifi",
        }
    if not force and prior.get(HOTSPOT_SETUP_FLAG, "") in (
        "success",
        "ap_not_supported",
        "no_wifi_hardware",
    ):
        return {
            "skipped": True,
            "reason": "already_configured",
            "flag": prior.get(HOTSPOT_SETUP_FLAG, ""),
        }

    iface = get_wifi_interface()
    if not iface:
        settings_manager.set_setting(HOTSPOT_SETUP_FLAG, "no_wifi_hardware")
        logger.info("Hotspot setup skipped: no Wi-Fi interface detected")
        return {
            "skipped": True,
            "reason": "no_wifi_hardware",
            "interface": None,
        }
    if not _wifi_ap_supported(iface):
        settings_manager.set_setting(HOTSPOT_SETUP_FLAG, "ap_not_supported")
        logger.info("Hotspot setup skipped: AP mode not supported on %s", iface)
        return {
            "skipped": True,
            "reason": "ap_not_supported",
            "interface": iface,
        }

    host = _ensure_host_settings(settings_manager)
    result = start_hotspot(host["host_ssid"], host["host_password"])

    if result.get("success"):
        settings_manager.set_setting(HOTSPOT_SETUP_FLAG, "success")
        ip_address = result.get("ip_address")
        if ip_address:
            settings_manager.set_setting("host_ip", ip_address)
        logger.info(
            "Hotspot enabled on install/first run: SSID=%s interface=%s",
            host["host_ssid"],
            result.get("interface") or iface,
        )
        return {
            "success": True,
            "ssid": host["host_ssid"],
            "interface": result.get("interface") or iface,
            "ip_address": ip_address or host["host_ip"],
            "enabled": True,
        }

    settings_manager.set_setting(HOTSPOT_SETUP_FLAG, "failed")
    logger.warning(
        "Hotspot setup failed on %s: %s",
        iface,
        result.get("error"),
    )
    return {
        "success": False,
        "interface": iface,
        "error": result.get("error"),
        "status": get_hotspot_status(),
    }
