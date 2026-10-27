# Component releases and upgrades

These changes are local and require review. No releases, tags, or repository
contents have been published by this implementation work.

## Release workflow

Use `.github/workflows/Publish-Release.yml` with:

- `release_type=beta`, `version=2.0.0-beta.01d`, and a human display name to move
  only the rolling `beta` tag and release.
- `release_type=numbered`, `version=v2.0.0-beta.01` (or a final version), and a
  display name to publish an immutable numbered release and advance `latest`.

Each release has its own `boondock-edge-api-TAG.tar.gz` and matching
`.tar.gz.sha256` file. The `latest` asset contains exactly the numbered build,
including that build's numbered `git_tag`; its filename uses `latest`.

The archive contains only `api/`, with `build-info.json` beside `manage.py`,
the Python app, the migrations, and the runtime root files. The workflow never
creates an Edge installation manifest. The version is machine-readable and is
separate from the display name. Historical beta identifiers with leading zeroes
and letter suffixes are supported.

## Persistent installation metadata

`DATA_ROOT/manifest.json` belongs to the installer. Its `api` and `dashboard`
entries contain the corresponding component's build information. Other entries
are preserved. File replacement is atomic and component updates take a shared
manifest lock to prevent lost updates.

`python manage.py upgrade` reads the new `build-info.json` beside itself and the
installed API version from the persistent manifest. No version arguments are
needed. A missing manifest, API entry, or API version runs all migrations. A
malformed JSON manifest or malformed nonempty version fails instead of silently
substituting another version.

Authorization and dashboard cutover migrations predate manifest tracking. They
are legacy migrations without an invented application version boundary and run
only when the installed API version is absent. Both tolerate retries. Future
migrations use independent `if current is None or current < parse_version(...)`
checks directly in `upgrade()`, in release order, using each migration's confirmed
introduction version. Every applicable migration runs sequentially; the manifest
is written only after all succeed. Migration identifiers and completion-table
tracking have been removed.
Standalone `manage.py upgrade` preserves the manifest on failure; the component
installer additionally restores files and SQLite databases on failure.

## API integration

The official repositories are used by default; no environment configuration is
required:

- API: `Boondock-Echo/Boondock-Edge-API`
- Dashboard: `Boondock-Echo/Boondock-Edge-Dashboard`

`BOONDOCK_API_REPOSITORY` and `BOONDOCK_DASHBOARD_REPOSITORY` remain optional
overrides for installations that use other repositories, in `owner/repository`
format.

The server uses GitHub's public release API. Private repository credentials are
not configured by this change. Requests are subject to GitHub's unauthenticated
rate limits.

All routes below require a current administrator user credential:

- `GET /api/version/status`: installed manifest and available rollback snapshots.
- `GET /api/version/releases?component=api` (or `dashboard`): installable release
  tags and display names, including `latest` and `beta` if present. Discovery
  examines up to ten pages of 100 GitHub releases.
- `POST /api/version/apply`: JSON `{"component":"api","tag":"beta"}`.
- `POST /api/version/rollback`: JSON `{"backup_id":"ID_FROM_INSTALL_RESPONSE"}`.
- `POST /api/version/restart`: no body or `{}`; retry a failed API restart.

The apply endpoint replaces the old combined ZIP upload contract. It accepts
only component and tag, resolves asset URLs from the configured repository, and
verifies the checksum before extraction. Client-supplied URLs are rejected.
Download redirects are restricted to HTTPS GitHub asset hosts. Limits apply to
download bytes, decompressed TAR bytes, total extracted file bytes, and member
count. TAR symlinks, hardlinks, devices, FIFOs, sparse files, unexpected types,
duplicate paths, and traversal are rejected. Extraction occurs in private
staging before live installation files are touched.

The dashboard repository must independently supply
`boondock-edge-dashboard-TAG.tar.gz` and its `.sha256` file. The archive contains
only `dashboard/`, with `index.html`, assets, and `build-info.json`. Build metadata
uses the same required fields as API builds, with `component="dashboard"`.
Dashboard publication and UI changes are outside this API checkout.

## Recovery and operational limits

Backups live in `DATA_ROOT/.release_backups/`, outside replaceable code. The five
most recent recorded snapshots are retained. API snapshots cover the managed API
files, its manifest entry, and SQLite `*.db` files in `Config.get_db_dir()` using
SQLite's backup API, including committed WAL data. Dashboard snapshots cover only
`build/` and its manifest entry. Rollback preserves the other component's files
and current manifest entry. An API install executes the newly installed
`manage.py upgrade` in a separate process with the same data root. Migration
failures, timeouts, and file application errors trigger restoration.

Installation and rollback requests are serialized. Snapshots are recorded before
changing live files so they remain available after an interrupted operation.
The privileged appliance installer must set up the fixed systemd restart helper
once, using `deploy/systemd/install-api-restart.sh` with the existing API unit
name. See [automatic restart deployment](deploy/systemd/README.md) for setup and
security details. Successful API installations and rollbacks then automatically
queue a delayed restart and return 202. Dashboard-only changes return 200 and do
not restart the API. Poll `/api/version/status` for restart completion. A failed
restart request can be retried using the administrator-only restart endpoint
without reinstalling files or rerunning migrations.

This is a synchronous installer; it does not stop background workers, coordinate
normal application writes, or install Python or operating system dependencies.
Use a maintenance window that quiesces other writers and pre-provision required
dependencies. Restoring an API database snapshot returns all
those databases to snapshot time. Forward migrations do not reverse database
changes when selecting an older numbered release; use a matching rollback
snapshot when restoring older code and schema together.

## Validation

Run the suite with an isolated data root:

```sh
BOONDOCK_DATA_ROOT=/tmp/boondock-release-tests WERKZEUG_RUN_MAIN=false python -m pytest -q
```

The focused release tests exercise version ordering, legacy migration retries,
installed-version handling, manifest preservation and atomic replacement,
concurrent component metadata writes, packaging shell steps, malicious TAR
rejection, download limits and checksums, administrator authorization, component
independence, file/database rollback, the installed manager subprocess, fixed
restart commands, restart authorization and cooldowns, and restart failure/retry
behavior.
GitHub discovery and downloads are mocked in tests; publishing has not been run.
