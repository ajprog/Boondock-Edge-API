"""Component build information and the installer's persistent Edge manifest."""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


def parse_version(value: str) -> tuple:
    """Order application versions, including beta.9, beta.10 and beta.01d.

    Numeric prerelease identifiers are compared numerically; a letter suffix
    sorts after its unsuffixed number. Final releases sort after prereleases.
    Leading zeroes in historical beta identifiers are deliberately accepted.
    """
    if not isinstance(value, str):
        raise ValueError("Version must be a string")
    match = re.fullmatch(
        r"v?(\d+)\.(\d+)\.(\d+)(?:-([a-zA-Z][a-zA-Z0-9-]*(?:\.[a-zA-Z0-9-]+)*))?",
        value,
    )
    if not match:
        raise ValueError(f"Invalid application version: {value!r}")
    major, minor, patch, prerelease = match.groups()
    identifiers = []
    for identifier in (prerelease or "").split("."):
        numeric = re.fullmatch(r"(\d+)([a-zA-Z]*)", identifier)
        identifiers.append(
            (0, int(numeric[1]), numeric[2]) if numeric else (1, identifier)
        )
    return int(major), int(minor), int(patch), prerelease is None, tuple(identifiers)


def read_json_object(path: Path, *, missing_ok: bool = False) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if missing_ok:
            return {}
        raise
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def validate_build_info(info: dict, component: str) -> dict:
    if component not in ("api", "dashboard") or info.get("component") != component:
        raise ValueError("Build information has the wrong component")
    for key in ("version", "display_name", "git_tag", "git_commit", "built_at", "repository"):
        if not isinstance(info.get(key), str) or not info[key].strip():
            raise ValueError(f"Missing or invalid build information: {key}")
    parse_version(info["version"])
    return info


def read_build_info(code_root: Path, component: str = "api") -> dict:
    return validate_build_info(read_json_object(code_root / "build-info.json"), component)


def atomic_write_json(path: Path, value: dict) -> None:
    """Replace a JSON document atomically using a file on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def manifest_lock(data_root: Path):
    """Serialize read/modify/write operations across processes and components."""
    data_root.mkdir(parents=True, exist_ok=True)
    with (data_root / ".manifest.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def update_component(data_root: Path, component: str, info: dict | None) -> None:
    """Replace one entry, preserving other components and future properties."""
    if component not in ("api", "dashboard"):
        raise ValueError("Unknown component")
    with manifest_lock(data_root):
        path = data_root / "manifest.json"
        manifest = read_json_object(path, missing_ok=True)
        if info is None:
            if component not in manifest:
                return
            manifest.pop(component, None)
        else:
            if manifest.get(component) == info:
                return
            manifest[component] = info
        atomic_write_json(path, manifest)
