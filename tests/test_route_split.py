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
from app.model_profiles import (
    ResolutionNotAllowedError,
    allowed_resolutions,
    apply_pinned_resolution,
    enforce_resolution,
    pinned_resolution,
)
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


class ResolutionPinningTests(unittest.TestCase):
    def test_a_route_with_a_single_resolution_always_pins_it(self):
        # A single configured resolution is both what the route publishes and
        # what it must produce, whatever its name looks like: MAI Token demands
        # an explicit ``resolution`` in the body, so leaving it to the upstream
        # default silently downgraded a 1080p route.
        self.assertEqual(pinned_resolution(["720p"]), "720p")
        self.assertEqual(pinned_resolution([" 720p ", ""]), "720p")
        self.assertEqual(pinned_resolution(["720P"]), "720P")
        # Several resolutions, none, or "自动" leave the choice to the caller.
        self.assertIsNone(pinned_resolution(["480p", "720p"]))
        self.assertIsNone(pinned_resolution([]))
        self.assertIsNone(pinned_resolution(None))
        self.assertIsNone(pinned_resolution(["自动"]))
        self.assertIsNone(pinned_resolution(["   "]))

    def test_missing_resolution_is_filled_and_an_explicit_one_is_kept(self):
        self.assertEqual(
            apply_pinned_resolution({"prompt": "p"}, ["720p"])["resolution"], "720p"
        )
        # An explicit resolution is left untouched: whether it is allowed is
        # decided once, by ``enforce_resolution``, from the same route list, so
        # a second copy of the rule cannot drift away from what is published.
        for payload in (
            {"resolution": "720P"},
            {"resolution": "1280x720"},
            {"metadata": {"resolution": "720p"}},
        ):
            with self.subTest(payload=payload):
                self.assertIs(apply_pinned_resolution(payload, ["720p"]), payload)
        payload = {"resolution": "1080p"}
        self.assertIs(apply_pinned_resolution(payload, ["720p", "1080p"]), payload)

    def test_a_different_resolution_is_rejected_by_the_route_list(self):
        for payload in (
            {"resolution": "1080p"},
            {"metadata": {"resolution": "480p"}},
            {"quality": "1080p"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ResolutionNotAllowedError, "只支持：720p"):
                    enforce_resolution(payload, ["720p"])
        # A quality word is not a resolution and must pass through.
        enforce_resolution({"quality": "high"}, ["720p"])

    def test_the_route_list_is_what_gets_enforced(self):
        for payload in (
            {},
            {"resolution": "1080p"},
            {"resolution": "1080P"},
            {"resolution": "1280x720"},
            {"metadata": {"resolution": "720p"}},
            {"metadata": {"parameters": {"resolution": "720P"}}},
            {"quality": "720p"},
            {"size": "1280x720"},
            {"size": "720x1280"},
        ):
            with self.subTest(payload=payload):
                enforce_resolution(payload, ["480p", "720p", "1080p"])

        rejected = {
            "resolution": {"resolution": "1080p"},
            "metadata.resolution": {"metadata": {"resolution": "2160p"}},
            "metadata.parameters.resolution": {"metadata": {"parameters": {"resolution": "1080p"}}},
            "quality": {"quality": "1080P"},
            "size": {"size": "3840x2160"},
        }
        for field, payload in rejected.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(ResolutionNotAllowedError, "只支持：480p、720p"):
                    enforce_resolution(payload, ["480p", "720p"])

        # A value that is not a resolution at all says so and lists the options.
        with self.assertRaisesRegex(ResolutionNotAllowedError, "必须是.*只支持：480p、720p"):
            enforce_resolution({"resolution": "4k"}, ["480p", "720p"])
        # A size that is not a video resolution is not mistaken for one.
        enforce_resolution({"size": "1280x32"}, ["720p"])

    def test_a_route_without_resolutions_is_not_limited(self):
        enforce_resolution({"resolution": "2160p"}, [])
        self.assertEqual(allowed_resolutions("sora2"), ["720p"])
        self.assertEqual(allowed_resolutions("sora2", ["480p", "720p"]), ["480p", "720p"])
        self.assertEqual(allowed_resolutions("sora2", [" 480p ", ""]), ["480p"])
        # "自动" means the gateway picks: not a value to pin, not a value to
        # reject a caller's explicit tier for.
        self.assertEqual(allowed_resolutions("sora2", ["自动"]), [])

    def test_autoDL_orientation_suffixes_are_not_mistaken_for_other_tiers(self):
        # AutoDL's workflows carry the orientation inside the resolution string
        # (``768p横`` / ``768p(1:1)``); rejecting them would break a channel that
        # works today, and collapsing them to ``768p`` would let a portrait
        # request pass as a landscape one.
        for payload in (
            {"resolution": "768p横"},
            {"resolution": "768p竖"},
            {"resolution": "768p(1:1)"},
            {"resolution": "480p横"},
            {"resolution": "768P横"},
        ):
            with self.subTest(payload=payload):
                enforce_resolution(payload, ["480p", "768p"])
        enforce_resolution({"resolution": "768p横"}, ["768p横", "768p竖"])
        for payload in (
            {"resolution": "1080p横"},
            {"resolution": "768p横"},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ResolutionNotAllowedError):
                    enforce_resolution(payload, ["768p竖"])
        # A single orientation-specific value is pinned verbatim.
        self.assertEqual(apply_pinned_resolution({}, ["768p横"])["resolution"], "768p横")


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

    def test_a_route_that_does_not_forward_resolution_still_rejects_another_tier(self):
        # The switch controls whether the value reaches the upstream body, not
        # whether the route keeps its promise. A route fixed at 720p must still
        # answer 400 instead of quietly returning a 720p clip to someone who
        # asked (and was billed) for 1080p.
        public_model = self._save(forward_resolution=False)
        with self.assertRaises(HTTPException) as raised:
            self._create({
                "model": public_model,
                "prompt": "p",
                "seconds": 5,
                "resolution": "1080p",
                "metadata": {"parameters": {"resolution": "1080P"}},
            })
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("只支持：720p", raised.exception.detail)

        sent = self._create({
            "model": public_model,
            "prompt": "p",
            "seconds": 5,
            "resolution": "720p",
            "metadata": {"parameters": {"resolution": "720P"}},
        })
        self.assertNotIn("resolution", sent["json"])
        self.assertNotIn("resolution", sent["json"].get("metadata", {}).get("parameters", {}))


    def test_a_rejected_resolution_never_reaches_the_upstream(self):
        public_model = self._save()
        with patch("app.proxy.httpx.AsyncClient") as client:
            with self.assertRaises(HTTPException) as raised:
                self._create({"model": public_model, "prompt": "p", "seconds": 5, "resolution": "1080p"})
            client.assert_not_called()
        self.assertEqual(raised.exception.status_code, 400)
        # The message names the value and every tier the model does support, so
        # the caller can fix the request without reading the docs.
        self.assertIn("1080p", raised.exception.detail)
        self.assertIn("只支持：720p", raised.exception.detail)


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
