"""Install independently versioned GitHub component TAR releases with rollback."""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import io
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zlib
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse

import requests

from config import Config, DATA_ROOT
from release_metadata import (
    atomic_write_json, manifest_lock, parse_version, read_build_info,
    read_json_object, update_component,
)

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_EXTRACTED_FILES = 50_000
BACKUP_RETENTION = 5
API_FILES = (
    "app", "migrations", "run.py", "config.py", "manage.py", "requirements.txt",
    "release_metadata.py", "build-info.json",
)


def api_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def backups_dir() -> Path:
    # Persistent state must survive replacement of application code.
    path = DATA_ROOT / ".release_backups"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _state_path() -> Path:
    return backups_dir() / "state.json"


def _load_state() -> dict:
    state = read_json_object(_state_path(), missing_ok=True)
    state.setdefault("backups", [])
    return state


@contextmanager
def _installation_lock():
    with (backups_dir() / ".install.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another installation or rollback is in progress") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def get_status() -> dict:
    return {
        "installed": read_json_object(DATA_ROOT / "manifest.json", missing_ok=True),
        "backups": _load_state()["backups"],
    }


def _repository(component: str) -> str:
    if component not in ("api", "dashboard"):
        raise ValueError("component must be api or dashboard")
    repo = getattr(Config, f"{component.upper()}_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError(f"Configure BOONDOCK_{component.upper()}_REPOSITORY as owner/repository")
    return repo


def _validate_tag(tag: str) -> str:
    if tag in ("latest", "beta"):
        return tag
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ValueError("Select latest, beta, or a numbered vX.Y.Z release tag")
    parse_version(tag)
    return tag


def _download(url: str, *, limit: int, asset: bool = False) -> bytes:
    """Bound downloads, including chunked responses, and constrain redirects."""
    deadline = time.monotonic() + 120
    for _ in range(5):
        parsed = urlparse(url)
        hosts = {"github.com", "release-assets.githubusercontent.com"} if asset else {"api.github.com"}
        if (parsed.scheme != "https" or parsed.hostname not in hosts
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError("Untrusted GitHub download URL")
        try:
            with requests.get(url, stream=True, timeout=(10, 30), allow_redirects=False,
                              headers={"Accept": "application/vnd.github+json"} if not asset else {}) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = response.headers.get("Location", "")
                    continue
                response.raise_for_status()
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > limit:
                    raise ValueError("Download exceeds size limit")
                result = bytearray()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if len(result) + len(chunk) > limit:
                        raise ValueError("Download exceeds size limit")
                    if time.monotonic() > deadline:
                        raise ValueError("Download exceeded time limit")
                    result.extend(chunk)
                return bytes(result)
        except requests.RequestException as exc:
            raise RuntimeError("Unable to retrieve the selected GitHub release") from exc
    raise ValueError("Too many GitHub download redirects")


def _github_json(path: str):
    import json
    return json.loads(_download(f"https://api.github.com/{path}", limit=4 * 1024 * 1024))


def _asset_names(component: str, tag: str) -> tuple[str, str]:
    name = f"boondock-edge-{component}-{tag}.tar.gz"
    return name, name + ".sha256"


def _assets(release: dict, component: str, tag: str) -> tuple[dict, dict]:
    result = []
    for name in _asset_names(component, tag):
        matches = [item for item in release.get("assets", []) if item.get("name") == name]
        if len(matches) != 1:
            raise ValueError(f"Release must contain exactly one {name}")
        result.append(matches[0])
    return tuple(result)


def list_releases(component: str) -> list[dict]:
    repo = _repository(component)
    result = []
    for page in range(1, 11):
        releases = _github_json(f"repos/{repo}/releases?per_page=100&page={page}")
        if not isinstance(releases, list):
            raise ValueError("Invalid GitHub release listing")
        for release in releases:
            if release.get("draft"):
                continue
            tag = release.get("tag_name", "")
            try:
                _validate_tag(tag)
                _assets(release, component, tag)
            except ValueError:
                continue
            result.append({"tag": tag, "display_name": release.get("name") or tag,
                           "prerelease": bool(release.get("prerelease")),
                           "published_at": release.get("published_at")})
        if len(releases) < 100:
            break
    return result


def _release_download(component: str, tag: str) -> tuple[bytes, str]:
    repo = _repository(component)
    _validate_tag(tag)
    release = _github_json(f"repos/{repo}/releases/tags/{quote(tag, safe='')}")
    if release.get("draft") or release.get("tag_name") != tag:
        raise ValueError("Selected release is unavailable")
    archive, checksum = _assets(release, component, tag)
    for asset in (archive, checksum):
        expected = f"https://github.com/{repo}/releases/download/{quote(tag, safe='')}/{asset['name']}"
        if asset.get("browser_download_url") != expected:
            raise ValueError("Release asset does not belong to the configured repository")
    checksum_data = _download(checksum["browser_download_url"], limit=4096, asset=True).decode("ascii")
    match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([^\r\n]+)\n?", checksum_data)
    if not match or match[2] != archive["name"]:
        raise ValueError("Invalid release checksum file")
    data = _download(archive["browser_download_url"], limit=MAX_DOWNLOAD_BYTES, asset=True)
    if hashlib.sha256(data).hexdigest() != match[1].lower():
        raise ValueError("Release checksum mismatch")
    return data, repo


class _LimitedReader:
    def __init__(self, stream, limit):
        self.stream = stream
        self.remaining = limit

    def read(self, size=-1):
        size = min(size if size >= 0 else self.remaining + 1, self.remaining + 1)
        data = self.stream.read(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ValueError("Decompressed TAR exceeds size limit")
        return data


def _safe_extract_tar(data: bytes, dest: Path) -> None:
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("Archive exceeds size limit")
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    total, count = 0, 0
    seen = set()
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
            # Also bound decompression of metadata and padding, not just file data.
            bounded = _LimitedReader(compressed, MAX_UNCOMPRESSED_BYTES + MAX_EXTRACTED_FILES * 2048)
            with tarfile.open(fileobj=bounded, mode="r|") as archive:
                for member in archive:
                    count += 1
                    if count > MAX_EXTRACTED_FILES:
                        raise ValueError("Too many files in archive")
                    name = member.name
                    parts = PurePosixPath(name).parts
                    if (not parts or PurePosixPath(name).is_absolute() or ".." in parts
                            or "\\" in name or re.match(r"^[A-Za-z]:", name)):
                        raise ValueError("Illegal path in archive")
                    target = dest.joinpath(*parts)
                    if not target.resolve().is_relative_to(dest) or target.is_symlink():
                        raise ValueError("Archive path escapes destination")
                    if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) or member.sparse is not None:
                        raise ValueError("Only ordinary files and directories are allowed")
                    relative = target.relative_to(dest)
                    if relative in seen:
                        raise ValueError("Duplicate archive path")
                    seen.add(relative)
                    if member.size < 0:
                        raise ValueError("Invalid TAR member size")
                    total += member.size
                    if total > MAX_UNCOMPRESSED_BYTES:
                        raise ValueError("Extracted archive exceeds size limit")
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.extractfile(member) as source, target.open("xb") as output:
                            shutil.copyfileobj(source, output, length=64 * 1024)
                        target.chmod(member.mode & 0o755)
            # Verify the gzip trailer and bound any padding after TAR end markers.
            while bounded.read(64 * 1024):
                pass
    except (tarfile.TarError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise ValueError("Invalid .tar.gz release archive") from exc


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _copy(source: Path, target: Path) -> None:
    _remove(target)
    if source.is_dir():
        shutil.copytree(source, target)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _component_paths(component: str) -> tuple[str, ...]:
    return API_FILES if component == "api" else ("build",)


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 120

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise RuntimeError("Timed out copying the SQLite database")

    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as origin:
        with closing(sqlite3.connect(target)) as destination:
            origin.backup(destination, pages=128, progress=progress, sleep=0.05)


def _snapshot(component: str, identifier: str) -> dict:
    snapshot = backups_dir() / identifier
    snapshot.mkdir(mode=0o700)
    files = snapshot / "files"
    files.mkdir()
    for name in _component_paths(component):
        source = api_root() / name
        if source.exists():
            _copy(source, files / name)
    with manifest_lock(DATA_ROOT):
        manifest = read_json_object(DATA_ROOT / "manifest.json", missing_ok=True)
        metadata = manifest.get(component)
    databases = []
    if component == "api":
        db_dir = Config.get_db_dir()
        for database in sorted(db_dir.glob("*.db")):
            if database.is_file():
                _sqlite_backup(database, snapshot / "databases" / database.name)
                databases.append(database.name)
    entry = {"id": identifier, "component": component, "metadata": metadata,
             "databases": databases, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    atomic_write_json(snapshot / "backup.json", entry)
    return entry


def _restore(entry: dict) -> None:
    component = entry["component"]
    snapshot = backups_dir() / entry["id"]
    for name in _component_paths(component):
        _copy(snapshot / "files" / name, api_root() / name)
    if component == "api":
        db_dir = Config.get_db_dir()
        for database in db_dir.glob("*.db"):
            if database.name not in entry["databases"]:
                _remove(database)
                for suffix in ("-wal", "-shm"):
                    _remove(Path(str(database) + suffix))
        for name in entry["databases"]:
            # SQLite backup restores safely even when WAL files exist.
            _sqlite_backup(snapshot / "databases" / name, db_dir / name)
    update_component(DATA_ROOT, component, entry["metadata"])


def _record_backup(entry: dict) -> None:
    state = _load_state()
    state["backups"] = [entry, *state["backups"]]
    expired = state["backups"][BACKUP_RETENTION:]
    state["backups"] = state["backups"][:BACKUP_RETENTION]
    atomic_write_json(_state_path(), state)
    for item in expired:
        shutil.rmtree(backups_dir() / item["id"], ignore_errors=True)


def _run_upgrade() -> None:
    environment = os.environ.copy()
    environment["BOONDOCK_DATA_ROOT"] = str(DATA_ROOT)
    try:
        completed = subprocess.run([sys.executable, "manage.py", "upgrade"], cwd=api_root(),
                                   env=environment, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("API migration timed out; restoring the previous installation") from exc
    if completed.returncode:
        raise RuntimeError("API migration failed; restoring the previous installation")


def install_release(component: str, tag: str) -> dict:
    # All URLs and expected checksums are resolved by the server, never the client.
    with _installation_lock():
        data, repository = _release_download(component, tag)
        return _apply_component(data, component, tag, repository)


def _apply_component(data: bytes, component: str, tag: str, repository: str) -> dict:
    if component not in ("api", "dashboard"):
        raise ValueError("Unknown component")
    with tempfile.TemporaryDirectory(prefix="bd_release_") as temporary:
        extract = Path(temporary)
        _safe_extract_tar(data, extract)
        if set(path.name for path in extract.iterdir()) != {component}:
            raise ValueError("Release must contain only the selected component directory")
        source = extract / component
        info = read_build_info(source, component)
        if info["repository"] != repository:
            raise ValueError("Build repository does not match the configured repository")
        if tag == "latest":
            if not info["git_tag"].startswith("v"):
                raise ValueError("latest must contain a numbered release build")
            _validate_tag(info["git_tag"])
        elif info["git_tag"] != tag:
            raise ValueError("Build tag does not match the selected release")
        if info["git_tag"].startswith("v") and parse_version(info["git_tag"]) != parse_version(info["version"]):
            raise ValueError("Build version does not match its numbered tag")
        if component == "api":
            for name in API_FILES:
                path = source / name
                valid = path.is_dir() if name in ("app", "migrations") else path.is_file()
                if not valid:
                    raise ValueError(f"Required API path missing: {name}")
        elif not (source / "index.html").is_file():
            raise ValueError("Dashboard release must contain index.html")
        identifier = uuid.uuid4().hex
        entry = _snapshot(component, identifier)
        # Persist rollback metadata before touching live files, for recovery after interruption.
        _record_backup(entry)
        try:
            if component == "api":
                for name in API_FILES:
                    _copy(source / name, api_root() / name)
                _run_upgrade()
            else:
                _copy(source, api_root() / "build")
                update_component(DATA_ROOT, component, info)
        except Exception:
            _restore(entry)
            raise
        return {"ok": True, "component": component, "installed": info,
                "pre_update_backup_id": identifier,
                "message": "Installed. Restart the API service to load updated code."}


def rollback(backup_id: str) -> dict:
    if not isinstance(backup_id, str) or not re.fullmatch(r"[0-9a-f]{32}", backup_id):
        raise ValueError("Invalid backup id")
    with _installation_lock():
        entry = next((item for item in _load_state()["backups"] if item["id"] == backup_id), None)
        if entry is None:
            raise ValueError("Backup not found")
        recovery = _snapshot(entry["component"], uuid.uuid4().hex)
        # Keep the requested snapshot until it has been restored.
        try:
            _restore(entry)
            _record_backup(recovery)
        except Exception:
            _restore(recovery)
            raise
        return {"ok": True, "component": entry["component"],
                "message": "Backup restored. Restart the API service to load restored code."}
