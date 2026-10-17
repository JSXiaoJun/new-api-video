from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
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

from app import database
from app.model_profiles import (
    DurationNotAllowedError,
    allowed_durations,
    capabilities_for,
    enforce_duration,
)
from app.proxy import create_video


class DurationCheckTests(unittest.TestCase):
    def test_route_durations_win_over_the_profile_default(self):
        self.assertEqual(allowed_durations("sora2"), [4, 8, 12])
        self.assertEqual(allowed_durations("sora2", [5, 10]), [5, 10])
        self.assertEqual(allowed_durations("sora2", 6), [6])
        # The published list and the enforced list are the same list.
        self.assertEqual(capabilities_for("sora2")["durations"], [4, 8, 12])
        self.assertEqual(capabilities_for("sora2", [5, 10])["durations"], [5, 10])

    def test_a_listed_duration_passes_in_any_numeric_form(self):
        for payload in (
            {"duration": 8},
            {"seconds": "8"},
            {"seconds": " 8 "},
            {"duration": 8.0},
            {"duration": 8, "seconds": "8"},
            {"metadata": {"parameters": {"duration": 8}}},
            {},
            {"duration": None, "seconds": ""},
        ):
            with self.subTest(payload=payload):
                enforce_duration(payload, [4, 8, 12])

    def test_an_unlisted_duration_is_rejected_with_the_options(self):
        for payload in (
            {"duration": 6},
            {"seconds": "15"},
            {"duration": 8, "seconds": 6},
            {"metadata": {"duration": 6}},
            {"metadata": {"parameters": {"duration": 6}}},
            {"duration": -1},
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(DurationNotAllowedError, "可选时长：4、8、12 秒"):
                    enforce_duration(payload, [4, 8, 12])

    def test_a_non_integer_duration_is_rejected(self):
        for value in ("8s", "abc", 8.5, True, [8]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(DurationNotAllowedError, "必须是整数秒"):
                    enforce_duration({"duration": value}, [8])

    def test_an_empty_list_means_no_limit(self):
        enforce_duration({"duration": 99}, [])

    def test_special_values_pass_without_being_published(self):
        enforce_duration({"seconds": -1}, [5], (-1,))
        with self.assertRaises(DurationNotAllowedError):
            enforce_duration({"seconds": 6}, [5], (-1,))

    def test_message_folds_consecutive_seconds_into_ranges(self):
        with self.assertRaisesRegex(DurationNotAllowedError, "可选时长：4–15、20 秒$"):
            enforce_duration({"duration": 3}, list(range(4, 16)) + [20])


class DurationProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()

    def _save(self, protocol: str, profile: str, durations: list[int], upstream_model: str) -> str:
        public_model = f"duration-{time.time_ns()}"
        database.save_upstream({
            "name": public_model,
            "base_url": "https://fuyao47.xyz" if protocol == "fuyao" else "https://upstream.example",
            "api_key": "secret",
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": public_model,
                "upstream_model": upstream_model,
                "protocol": protocol,
                "profile": profile,
                "durations": durations,
                "resolutions": [],
                "image_count": 1,
                "video_count": 0,
                "audio_count": 0,
            }],
        })
        return public_model

    def _create(self, payload: dict) -> list[dict]:
        sent: list[dict] = []

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                sent.append(kwargs["json"])
                return httpx.Response(
                    200, request=httpx.Request("POST", url),
                    json={"id": f"task_{time.time_ns()}", "status": "queued"},
                )

        with patch("app.proxy.httpx.AsyncClient", MockAsyncClient):
            asyncio.run(create_video(payload, None))
        return sent

    def test_unlisted_route_duration_never_reaches_the_upstream(self):
        model = self._save("videos", "default", [5, 10], "upstream-video")
        with patch("app.proxy.httpx.AsyncClient") as client:
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(create_video({"model": model, "prompt": "p", "seconds": 7}, None))
            client.assert_not_called()
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("5、10 秒", raised.exception.detail)
        sent = self._create({"model": model, "prompt": "p", "seconds": 10})
        self.assertEqual(sent[0]["duration"], 10)

    def test_route_without_durations_uses_the_profile_list(self):
        model = self._save("videos", "sora2", [], "sora-2")
        with self.assertRaises(HTTPException) as raised:
            self._create({"model": model, "prompt": "p", "duration": 6})
        self.assertIn("4、8、12 秒", raised.exception.detail)
        self.assertEqual(self._create({"model": model, "prompt": "p", "duration": 8})[0]["duration"], 8)

    def test_request_without_a_duration_is_left_to_the_upstream(self):
        model = self._save("videos", "default", [5, 10], "upstream-video")
        self.assertNotIn("duration", self._create({"model": model, "prompt": "p"})[0])

    def test_wan3_automatic_duration_is_still_accepted(self):
        model = self._save("fuyao", "fuyao-wan3", [5], "wan3.0-video")
        sent = self._create({"model": model, "prompt": "p", "seconds": -1})
        self.assertEqual(sent[0]["metadata"]["parameters"]["duration"], -1)
        with self.assertRaises(HTTPException):
            self._create({"model": model, "prompt": "p", "seconds": 6})

    def test_ark_automatic_duration_is_still_accepted(self):
        model = self._save("ark-v3", "ark-seedance-2", [], "doubao-seedance-2-0")
        self.assertEqual(self._create({"model": model, "prompt": "p", "duration": -1})[0]["duration"], -1)
        with self.assertRaises(HTTPException) as raised:
            self._create({"model": model, "prompt": "p", "duration": 16})
        self.assertIn("4–15 秒", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
