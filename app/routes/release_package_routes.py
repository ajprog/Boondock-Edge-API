"""Administrator-only component release discovery, installation, and rollback."""
import logging

from flask import Blueprint, jsonify, request

from ..middleware.auth_middleware import require_admin
from ..services import release_package_service

log = logging.getLogger(__name__)
release_package_bp = Blueprint("release_package", __name__)


def _respond(operation):
    try:
        return jsonify(operation())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except (OSError, RuntimeError) as exc:
        log.exception("Release operation failed")
        return jsonify({"error": "Release operation failed; inspect the service log"}), 500


@release_package_bp.route("/version/status", methods=["GET"])
@require_admin
def version_status():
    return _respond(release_package_service.get_status)


@release_package_bp.route("/version/releases", methods=["GET"])
@require_admin
def version_releases():
    component = request.args.get("component", "api")
    return _respond(lambda: {"component": component,
                             "releases": release_package_service.list_releases(component)})


@release_package_bp.route("/version/apply", methods=["POST"])
@require_admin
def version_apply():
    data = request.get_json(silent=True)
    if (not isinstance(data, dict) or set(data) != {"component", "tag"}
            or not all(isinstance(value, str) for value in data.values())):
        return jsonify({"error": "Provide only component and tag as JSON strings"}), 400
    return _respond(lambda: release_package_service.install_release(data["component"], data["tag"]))


@release_package_bp.route("/version/rollback", methods=["POST"])
@require_admin
def version_rollback():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("backup_id"), str):
        return jsonify({"error": "backup_id is required"}), 400
    return _respond(lambda: release_package_service.rollback(data["backup_id"]))
