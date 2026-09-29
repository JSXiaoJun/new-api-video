from __future__ import annotations

import os
import re
import tempfile
import time
import unittest
from pathlib import Path


os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-with-more-than-32-chars")
os.environ.setdefault("ADAPTER_API_KEY", "test-adapter-key")
os.environ.setdefault("ENCRYPTION_KEY", "IougsRYbjtzQcNSrzLV2O-TQ3k1PDP69XcfdR3Lxp3I=")
TEST_DATA_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("DATA_DIR", TEST_DATA_DIR.name)

from fastapi.testclient import TestClient

from app.config import ROOT_DIR
from app.main import app
from app.security import SESSION_COOKIE, create_session
from app.static_assets import IMMUTABLE, REVALIDATE, asset_url, asset_version


ASSET_REF = re.compile(r'(?:href|src)="(/static/[^"]+)"')


class StaticAssetVersioningTests(unittest.TestCase):
    def test_templates_never_hand_write_static_urls(self):
        # A hand-written ``?v=`` is only bumped when someone remembers to; a
        # forgotten bump keeps browsers on the old script. Every reference must
        # go through ``asset_url`` so the URL follows the file's content.
        for template in (ROOT_DIR / "templates").glob("*.html"):
            text = template.read_text(encoding="utf-8")
            self.assertNotIn("/static/", text, template.name)

    def test_rendered_pages_reference_the_current_content_hash(self):
        client = TestClient(app)
        pages = {"/admin/login": client.get("/admin/login")}
        client.cookies.set(SESSION_COOKIE, create_session("admin"))
        pages["/admin"] = client.get("/admin")
        pages["/admin/images"] = client.get("/admin/images")
        for path, response in pages.items():
            self.assertEqual(response.status_code, 200, path)
            # A cached page would pin the old asset URLs.
            self.assertEqual(response.headers["cache-control"], "no-store", path)
            refs = ASSET_REF.findall(response.text)
            self.assertTrue(refs, path)
            for ref in refs:
                name, _, query = ref.removeprefix("/static/").partition("?v=")
                self.assertEqual(query, asset_version(name), ref)

    def test_only_the_current_version_is_cached_forever(self):
        client = TestClient(app)
        current = client.get(asset_url("admin.js"))
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.headers["cache-control"], IMMUTABLE)
        for stale in ("/static/admin.js?v=20260921-3", "/static/admin.js"):
            response = client.get(stale)
            self.assertEqual(response.status_code, 200, stale)
            self.assertEqual(response.headers["cache-control"], REVALIDATE, stale)
        self.assertEqual(client.get("/static/missing.js").status_code, 404)

    def test_version_follows_file_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            static_dir = Path(tmp).resolve()
            asset = static_dir / "app.js"
            asset.write_text("console.log(1)", encoding="utf-8")
            first = asset_version("app.js", static_dir)
            # Guarantee a new mtime even on filesystems with coarse timestamps.
            time.sleep(0.01)
            asset.write_text("console.log(2)", encoding="utf-8")
            os.utime(asset, ns=(time.time_ns(), time.time_ns() + 1_000_000))
            second = asset_version("app.js", static_dir)
            self.assertIsNotNone(first)
            self.assertNotEqual(first, second)
            self.assertIsNone(asset_version("../outside.js", static_dir))
            self.assertIsNone(asset_version("missing.js", static_dir))

    def test_unknown_asset_fails_the_render(self):
        with self.assertRaises(FileNotFoundError):
            asset_url("does-not-exist.js")


if __name__ == "__main__":
    unittest.main()
