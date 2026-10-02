from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from unittest.mock import patch

import httpx


os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-with-more-than-32-chars")
os.environ.setdefault("ADAPTER_API_KEY", "test-adapter-key")
os.environ.setdefault("ENCRYPTION_KEY", "IougsRYbjtzQcNSrzLV2O-TQ3k1PDP69XcfdR3Lxp3I=")
TEST_DATA_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("DATA_DIR", TEST_DATA_DIR.name)

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import database
from app.main import app
from app.model_profiles import ResolutionMismatchError, apply_pinned_resolution, pinned_resolution
from app.proxy import create_video
from app.schemas import UpstreamInput


def _route(model: str, resolutions: list[str], **extra) -> dict:
    return {
        "model": model,
        "upstream_model": "wan3.0-video",
        "protocol": "fuyao",
        "profile": "fuyao-wan3",
        "durations": [5],
        "resolutions": resolutions,
        "image_count": 1,
        "video_count": 0,
        "audio_count": 0,
        **extra,
    }


SPLIT = ("wan3.0-video-720p", "wan3.0-video")


class ResolutionPinningTests(unittest.TestCase):
    def test_only_a_split_shaped_route_is_pinned(self):
        self.assertEqual(pinned_resolution(["720p"], *SPLIT), "720p")
        self.assertEqual(pinned_resolution([" 720p ", ""], "WAN-720P", "wan"), "720p")
        self.assertIsNone(pinned_resolution(["480p", "720p"], *SPLIT))
        self.assertIsNone(pinned_resolution([], *SPLIT))
        self.assertIsNone(pinned_resolution(["自动"], *SPLIT))
        # The name does not promise a resolution.
        self.assertIsNone(pinned_resolution(["720p"], "wan3.0-video", "wan3.0-video"))
        # The upstream model already carries it (e.g. MAI Token ``sd-2.0-720p``,
        # which ignores ``resolution``): nothing to pin.
        self.assertIsNone(pinned_resolution(["720p"], "sd-2.0-720p", "sd-2.0-720p"))

    def test_missing_resolution_is_filled_and_a_match_is_kept(self):
        self.assertEqual(
            apply_pinned_resolution({"prompt": "p"}, ["720p"], *SPLIT)["resolution"], "720p"
        )
        self.assertEqual(
            apply_pinned_resolution({"resolution": "720P"}, ["720p"], *SPLIT)["resolution"], "720p"
        )
        payload = {"resolution": "1080p"}
        self.assertIs(apply_pinned_resolution(payload, ["720p", "1080p"], *SPLIT), payload)
        self.assertIs(apply_pinned_resolution(payload, ["720p"], "sd-2.0-720p", "sd-2.0-720p"), payload)

    def test_a_different_resolution_is_rejected(self):
        for payload in (
            {"resolution": "1080p"},
            {"metadata": {"resolution": "480p"}},
            {"quality": "1080p"},
        ):
            with self.assertRaises(ResolutionMismatchError, msg=payload):
                apply_pinned_resolution(payload, ["720p"], *SPLIT)
        # A quality word is not a resolution and must pass through.
        self.assertEqual(
            apply_pinned_resolution({"quality": "high"}, ["720p"], *SPLIT)["quality"], "high"
        )


class SharedUpstreamModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()

    def test_split_routes_may_share_one_upstream_model(self):
        upstream = UpstreamInput(
            name="split",
            base_url="https://fuyao47.xyz",
            api_key="key",
            routes=[_route("wan3.0-video-480p", ["480p"]), _route("wan3.0-video-720p", ["720p"])],
        )
        saved = database.save_upstream(upstream.model_dump())
        by_model = {route["model"]: route for route in saved["routes"]}
        self.assertEqual(by_model["wan3.0-video-480p"]["upstream_model"], "wan3.0-video")
        self.assertEqual(by_model["wan3.0-video-720p"]["upstream_model"], "wan3.0-video")
        self.assertEqual(database.select_upstream("wan3.0-video-720p")["resolutions"], ["720p"])

    def test_public_names_must_still_be_unique(self):
        with self.assertRaises(ValueError):
            UpstreamInput(
                name="dup",
                base_url="https://fuyao47.xyz",
                api_key="key",
                routes=[_route("same", ["480p"]), _route("same", ["720p"])],
            )

    def test_startup_drops_the_legacy_unique_index(self):
        # An existing deployment still has the index; startup must remove it,
        # otherwise saving split routes fails with an IntegrityError.
        # ``ignore_cleanup_errors``: SQLite's WAL files can stay locked briefly
        # on Windows after the last connection closes.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            legacy_db = os.path.join(tmp, "adapter.db")
            with patch.object(database, "DB_PATH", legacy_db):
                database.initialize()
                with closing(sqlite3.connect(legacy_db)) as conn:
                    conn.execute(
                        "CREATE UNIQUE INDEX idx_model_routes_upstream_model "
                        "ON model_routes(upstream_id, upstream_model)"
                    )
                    conn.commit()
                database.initialize()
                with closing(sqlite3.connect(legacy_db)) as conn:
                    index = conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'index' AND name = 'idx_model_routes_upstream_model'"
                    ).fetchone()
        self.assertIsNone(index)


class SplitRouteProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()

    def _save(self, **extra) -> str:
        # Split-shaped: ``<name>-720p`` mapped to the bare ``wan3.0-video``.
        public_model = f"wan3.0-video-{time.time_ns()}-720p"
        database.save_upstream({
            "name": public_model,
            "base_url": "https://fuyao47.xyz",
            "api_key": "secret",
            "enabled": True,
            "priority": 1,
            "routes": [_route(public_model, ["720p"], **extra)],
        })
        return public_model

    def _create(self, payload: dict) -> dict:
        captured: dict = {}

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                captured["json"] = kwargs["json"]
                return httpx.Response(
                    200, request=httpx.Request("POST", url),
                    json={"id": f"task_{time.time_ns()}", "status": "queued"},
                )

        with patch("app.proxy.httpx.AsyncClient", MockAsyncClient):
            asyncio.run(create_video(payload, None))
        return captured

    def test_split_route_sends_its_own_resolution(self):
        public_model = self._save()
        sent = self._create({"model": public_model, "prompt": "p", "seconds": 5})
        self.assertEqual(sent["json"]["model"], "wan3.0-video")
        self.assertEqual(sent["json"]["metadata"]["parameters"]["resolution"], "720P")

    def test_split_route_checks_metadata_parameters_resolution(self):
        public_model = self._save()
        with self.assertRaises(HTTPException) as raised:
            self._create({
                "model": public_model,
                "prompt": "p",
                "seconds": 5,
                "metadata": {"parameters": {"resolution": "1080P"}},
            })
        self.assertEqual(raised.exception.status_code, 400)
        sent = self._create({
            "model": public_model,
            "prompt": "p",
            "seconds": 5,
            "metadata": {"parameters": {"resolution": "720P"}},
        })
        self.assertEqual(sent["json"]["metadata"]["parameters"]["resolution"], "720P")

    def test_split_route_rejects_another_resolution(self):
        public_model = self._save()
        with self.assertRaises(HTTPException) as raised:
            self._create({"model": public_model, "prompt": "p", "seconds": 5, "resolution": "1080p"})
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("720p", raised.exception.detail)

    def test_route_that_does_not_forward_resolution_is_not_pinned(self):
        public_model = self._save(forward_resolution=False)
        sent = self._create({
            "model": public_model,
            "prompt": "p",
            "seconds": 5,
            "resolution": "1080p",
            "metadata": {"parameters": {"resolution": "1080P"}},
        })
        self.assertNotIn("resolution", sent["json"])
        self.assertNotIn("resolution", sent["json"].get("metadata", {}).get("parameters", {}))


class SplitButtonTests(unittest.TestCase):
    def test_route_editor_ships_the_split_button(self):
        client = TestClient(app)
        script = client.get("/static/admin.js").text
        self.assertIn("data-route-split", script)
        self.assertIn("function splitRouteRow", script)
        # The old client-side rule would reject every split before saving.
        self.assertNotIn("同一个上游模型不能重复映射", script)


if __name__ == "__main__":
    unittest.main()
