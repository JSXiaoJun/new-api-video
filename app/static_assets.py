"""Content-hashed URLs for the admin console's static files.

Templates must never hand-write ``/static/...?v=...``. A manual version string
is only bumped when someone remembers to, and a forgotten bump keeps browsers
and CDNs on the old script: the backend and the page then disagree, e.g. a new
protocol that the API accepts but the route editor cannot offer.

``asset_url("admin.js")`` derives the version from the file's bytes instead,
so any edit changes the URL. Responses for the current version are cached
forever (the URL can never point to other content); any other request must
revalidate, so a stale or missing version can at worst cost a round trip.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import parse_qs

from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from .config import ROOT_DIR


STATIC_DIR = (ROOT_DIR / "static").resolve()
STATIC_PREFIX = "/static"
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"

# path -> ((mtime_ns, size), version); recomputed only when the file changes.
_versions: dict[Path, tuple[tuple[int, int], str]] = {}


def _resolve(relative_path: str, static_dir: Path = STATIC_DIR) -> Path | None:
    candidate = (static_dir / relative_path.lstrip("/")).resolve()
    if not candidate.is_relative_to(static_dir) or not candidate.is_file():
        return None
    return candidate


def asset_version(relative_path: str, static_dir: Path = STATIC_DIR) -> str | None:
    """Short content hash of a static file, or ``None`` if it does not exist."""
    path = _resolve(relative_path, static_dir)
    if path is None:
        return None
    stat = path.stat()
    key = (stat.st_mtime_ns, stat.st_size)
    cached = _versions.get(path)
    if cached is not None and cached[0] == key:
        return cached[1]
    version = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    _versions[path] = (key, version)
    return version


def asset_url(relative_path: str) -> str:
    """URL for a template; a missing file fails the render instead of 404ing later."""
    version = asset_version(relative_path)
    if version is None:
        raise FileNotFoundError(f"static asset not found: {relative_path}")
    return f"{STATIC_PREFIX}/{relative_path.lstrip('/')}?v={version}"


class VersionedStaticFiles(StaticFiles):
    """Serve ``/static`` with cache headers that match :func:`asset_url`."""

    async def get_response(self, path: str, scope: Scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            requested = (query.get("v") or [None])[0]
            current = asset_version(path)
            response.headers["Cache-Control"] = (
                IMMUTABLE if requested and requested == current else REVALIDATE
            )
        return response
