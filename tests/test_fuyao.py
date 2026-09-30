from __future__ import annotations

import asyncio
import json
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
from fastapi.responses import Response
from fastapi.testclient import TestClient

from app import database
from app.channels import fuyao
from app.main import app
from app.model_profiles import capabilities_for, media_reference_counts, suggest_route
from app.proxy import create_video, fetch_task, stream_content
from app.security import SESSION_COOKIE, create_session, csrf_token


H3_10S = "Minimax-H3-768p-933-10s"
H3_10_15S = "Minimax-H3-768p-933-10s-15s"
SD_MINI = "【官方稳定版】sd2.0-480p-mini"
SD_FULL = "【官方稳定版】sd2.0-720p-满血"


def _mock_client(captured: dict, *, post_json=None, get_json=None, get_status=200):
    class MockAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured["post"] = (url, kwargs)
            return httpx.Response(200, request=httpx.Request("POST", url), json=post_json)

        async def get(self, url, **kwargs):
            captured.setdefault("get", []).append((url, kwargs))
            return httpx.Response(get_status, request=httpx.Request("GET", url), json=get_json)

    return MockAsyncClient


class FuyaoCatalogTests(unittest.TestCase):
    def test_base_url_detection_is_host_exact(self):
        self.assertTrue(fuyao.is_fuyao_base_url("https://fuyao47.xyz"))
        self.assertTrue(fuyao.is_fuyao_base_url("https://www.fuyao47.xyz/"))
        self.assertFalse(fuyao.is_fuyao_base_url("https://fuyao47.xyz.evil.example"))
        self.assertFalse(fuyao.is_fuyao_base_url("https://api.mai-token.com"))

    def test_documented_models_map_to_family_profiles(self):
        expected = {
            "grok-imagine-video-1.5": "fuyao-grok",
            "grok-imagine-video-1.5（zj）": "fuyao-grok",
            H3_10S: "fuyao-minimax-h3",
            "MiniMax-H3-933-1440p-10-15s": "fuyao-minimax-h3",
            SD_MINI: "fuyao-sd2",
            "【官方稳定版】sd2.0-720p-fast": "fuyao-sd2",
            "wan3.0-video": "fuyao-wan3",
            "wan3.0-video-prime": "fuyao-wan3",
            "seedance2.0-903": "fuyao-video",
            "Seedance2.5xg": "fuyao-video",
            "video-editor-fixed-0.5": "fuyao-video",
        }
        for model, profile in expected.items():
            self.assertEqual(fuyao.suggest_route(model)["profile"], profile, model)
            self.assertIn(profile, fuyao.PROFILE_DEFINITIONS)

    def test_route_limits_follow_the_model_id(self):
        self.assertEqual(fuyao.suggest_route(H3_10S)["durations"], [10])
        self.assertEqual(fuyao.suggest_route(H3_10_15S)["durations"], [10, 15])
        self.assertEqual(fuyao.suggest_route("MiniMax-H3-933-1440p-10-15s")["resolutions"], ["1440p"])
        self.assertEqual(fuyao.suggest_route(SD_MINI)["resolutions"], ["480p"])
        self.assertEqual(fuyao.suggest_route(SD_FULL)["resolutions"], ["720p"])
        self.assertEqual(fuyao.suggest_route("wan3.0-video")["durations"], list(range(2, 31)))

    def test_suggested_counts_advertise_only_supported_media(self):
        grok = suggest_route("grok-imagine-video-1.5", fuyao.PROTOCOL)
        self.assertEqual((grok["image_count"], grok["video_count"], grok["audio_count"]), (7, 0, 0))
        wan = suggest_route("wan3.0-video", fuyao.PROTOCOL)
        self.assertEqual((wan["image_count"], wan["video_count"], wan["audio_count"]), (10, 5, 5))
        generic = suggest_route("Seedance2.5xg", fuyao.PROTOCOL)
        self.assertEqual((generic["image_count"], generic["video_count"], generic["audio_count"]), (1, 0, 0))
        caps = capabilities_for("fuyao-minimax-h3", max_images=9, max_videos=3, max_audios=3)
        self.assertEqual(caps["maxReferences"], 12)
        self.assertTrue(caps["referenceVideo"])

    def test_video_model_filter_prefers_endpoint_types(self):
        self.assertTrue(fuyao.is_video_model("anything", ["openai-video"]))
        self.assertFalse(fuyao.is_video_model("wan3.0-video", ["openai"]))
        self.assertTrue(fuyao.is_video_model(SD_MINI))
        self.assertTrue(fuyao.is_video_model("Seedance2.5xg"))
        self.assertFalse(fuyao.is_video_model("gpt-4o-mini"))
        self.assertFalse(fuyao.is_video_model("gpt-image-1"))


class FuyaoPayloadTests(unittest.TestCase):
    def test_grok_single_image_uses_the_image_object_and_integer_seconds(self):
        body = fuyao.transform_create_payload({
            "model": "grok-imagine-video-1.5",
            "prompt": "镜头缓慢推进",
            "seconds": "6",
            "resolution": "480p",
            "image_url": "https://cdn.example/ref.jpg",
            "seed": 1,
        }, "fuyao-grok")
        self.assertEqual(body, {
            "model": "grok-imagine-video-1.5",
            "prompt": "镜头缓慢推进",
            "seconds": 6,
            "resolution": "480p",
            "image": {"url": "https://cdn.example/ref.jpg"},
        })

    def test_grok_multiple_images_use_reference_images(self):
        body = fuyao.transform_create_payload({
            "model": "grok-imagine-video-1.5",
            "prompt": "p",
            "aspect_ratio": "16:9",
            "image_urls": ["https://cdn.example/1.jpg", {"url": "https://cdn.example/2.jpg"}],
        })
        self.assertEqual(body["reference_images"], [
            {"url": "https://cdn.example/1.jpg", "role": "reference_image"},
            {"url": "https://cdn.example/2.jpg", "role": "reference_image"},
        ])
        self.assertNotIn("image", body)
        self.assertEqual(body["aspect_ratio"], "16:9")

    def test_grok_rejects_what_it_cannot_serve(self):
        cases = [
            {"reference_videos": ["https://cdn.example/v.mp4"]},
            {"last_frame_url": "https://cdn.example/last.jpg"},
            {
                "first_frame_url": "https://cdn.example/first.jpg",
                "image_urls": ["https://cdn.example/ref.jpg"],
            },
        ]
        for extra in cases:
            with self.assertRaises(fuyao.FuyaoRequestError, msg=extra):
                fuyao.transform_create_payload(
                    {"model": "grok-imagine-video-1.5", "prompt": "p", **extra}, "fuyao-grok"
                )
        # A lone first frame is the documented exception.
        body = fuyao.transform_create_payload({
            "model": "grok-imagine-video-1.5",
            "prompt": "p",
            "first_frame_url": "https://cdn.example/first.jpg",
        })
        self.assertEqual(
            body["reference_images"], [{"url": "https://cdn.example/first.jpg", "role": "first_frame"}]
        )

    def test_minimax_h3_seconds_must_match_the_model_id(self):
        body = fuyao.transform_create_payload(
            {"model": H3_10S, "prompt": "p", "resolution": "1080p", "size": "1920x1080"}
        )
        self.assertEqual(body["seconds"], "10")
        self.assertEqual(body["aspect_ratio"], "16:9")
        self.assertNotIn("resolution", body)
        self.assertNotIn("size", body)
        with self.assertRaises(fuyao.FuyaoRequestError):
            fuyao.transform_create_payload({"model": H3_10S, "prompt": "p", "seconds": 15})
        # Two allowed durations: nothing to infer, so seconds is required.
        with self.assertRaises(fuyao.FuyaoRequestError):
            fuyao.transform_create_payload({"model": H3_10_15S, "prompt": "p"})
        self.assertEqual(
            fuyao.transform_create_payload({"model": H3_10_15S, "prompt": "p", "seconds": 15})["seconds"],
            "15",
        )

    def test_sd2_mini_refuses_text_only_and_drops_resolution(self):
        with self.assertRaises(fuyao.FuyaoRequestError):
            fuyao.transform_create_payload({"model": SD_MINI, "prompt": "p", "seconds": 5})
        body = fuyao.transform_create_payload({
            "model": SD_MINI,
            "prompt": "p",
            "seconds": 5,
            "resolution": "720p",
            "images": ["https://cdn.example/ref.jpg"],
        })
        self.assertNotIn("resolution", body)
        self.assertEqual(body["seconds"], "5")
        self.assertEqual(body["reference_images"][0]["role"], "reference_image")
        # The full model accepts text-to-video.
        self.assertNotIn(
            "reference_images",
            fuyao.transform_create_payload({"model": SD_FULL, "prompt": "p", "seconds": 5}),
        )

    def test_wan3_orders_frames_and_forwards_every_media_kind(self):
        body = fuyao.transform_create_payload({
            "model": "wan3.0-video",
            "prompt": "p",
            "duration": 12,
            "resolution": "1080p",
            "reference_images": [
                {"url": "https://cdn.example/ref.jpg", "role": "reference_image"},
                {"url": "https://cdn.example/last.jpg", "role": "last_frame"},
            ],
            "first_frame": {"url": "https://cdn.example/first.jpg"},
            "video_urls": ["https://cdn.example/v.mp4"],
            "metadata": {"audio_urls": ["https://cdn.example/a.mp3"]},
        }, "fuyao-wan3")
        self.assertEqual(body["reference_images"], [
            {"url": "https://cdn.example/first.jpg", "role": "first_frame"},
            {"url": "https://cdn.example/last.jpg", "role": "last_frame"},
            {"url": "https://cdn.example/ref.jpg", "role": "reference_image"},
        ])
        self.assertEqual(body["reference_videos"], ["https://cdn.example/v.mp4"])
        self.assertEqual(body["reference_audios"], ["https://cdn.example/a.mp3"])
        self.assertEqual(body["resolution"], "1080p")
        self.assertEqual(body["seconds"], "12")

    def test_forwarded_media_matches_what_the_relay_counts(self):
        payload = {
            "model": "wan3.0-video",
            "prompt": "p",
            "image": {"url": "https://cdn.example/a.jpg"},
            "images": ["https://cdn.example/b.jpg"],
            "reference_images": [{"url": "https://cdn.example/c.jpg", "role": "reference_image"}],
            "reference_audios": ["https://cdn.example/a.mp3"],
            "reference_video": "https://cdn.example/v.mp4",
        }
        body = fuyao.transform_create_payload(payload)
        counts = media_reference_counts(payload)
        self.assertEqual(len(body["reference_images"]), counts["image"])
        self.assertEqual(len(body["reference_videos"]), counts["video"])
        self.assertEqual(len(body["reference_audios"]), counts["audio"])

    def test_conflicting_or_fractional_seconds_are_rejected(self):
        for extra in ({"seconds": 5, "duration": 6}, {"seconds": "5.5"}, {"seconds": "abc"}):
            with self.assertRaises(fuyao.FuyaoRequestError, msg=extra):
                fuyao.transform_create_payload({"model": SD_FULL, "prompt": "p", **extra})

    def test_generic_models_forward_size_and_resolution(self):
        body = fuyao.transform_create_payload({
            "model": "video-editor-fixed-0.5",
            "prompt": "p",
            "seconds": 8,
            "size": "1280x720",
            "resolution": "720p",
        })
        self.assertEqual(body, {
            "model": "video-editor-fixed-0.5",
            "prompt": "p",
            "seconds": "8",
            "aspect_ratio": "16:9",
            "resolution": "720p",
            "size": "1280x720",
        })


class FuyaoTaskTests(unittest.TestCase):
    def test_documented_statuses_normalize(self):
        expected = {
            "queued": "queued", "pending": "queued",
            "in_progress": "processing", "processing": "processing",
            "completed": "completed", "done": "completed",
            "failed": "failed", "error": "failed",
        }
        for upstream, relay in expected.items():
            self.assertEqual(fuyao.normalize_status(upstream), relay, upstream)

    def test_completed_task_downloads_through_content_not_the_temporary_url(self):
        fields = fuyao.extract_task_fields(
            {"status": "done", "progress": "100%", "video_url": "https://tmp.example/v.mp4"}, "task/1"
        )
        self.assertEqual(fields["status"], "completed")
        self.assertEqual(fields["video_url"], "/v1/videos/task%2F1/content")
        self.assertEqual(fields["progress"], 100)
        running = fuyao.extract_task_fields({"status": "in_progress", "progress": "56%"}, "t")
        self.assertIsNone(running["video_url"])
        self.assertEqual(running["progress"], 56)
        failed = fuyao.extract_task_fields({"status": "error", "message": "bad prompt"}, "t")
        self.assertEqual((failed["status"], failed["error"]), ("failed", "bad prompt"))


class FuyaoIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()

    def _save_route(self, upstream_model: str, profile: str, **counts) -> str:
        public_model = f"fuyao-public-{time.time_ns()}"
        upstream = database.save_upstream({
            "name": public_model,
            "base_url": "https://fuyao47.xyz",
            "api_key": "fuyao-secret",
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": public_model,
                "upstream_model": upstream_model,
                "protocol": fuyao.PROTOCOL,
                "profile": profile,
                "durations": list(range(4, 16)),
                "resolutions": ["480p", "720p"],
                "image_count": counts.get("image_count", 7),
                "video_count": counts.get("video_count", 0),
                "audio_count": counts.get("audio_count", 0),
            }],
        })
        self.assertEqual(upstream["routes"][0]["protocol"], fuyao.PROTOCOL)
        return public_model

    def test_discovery_keeps_only_video_models(self):
        captured: dict = {}
        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        mock = _mock_client(captured, get_json={"data": [
            {"id": "gpt-4o-mini", "supported_endpoint_types": ["openai"]},
            {"id": "grok-imagine-video-1.5", "supported_endpoint_types": ["openai-video"]},
            {"id": "wan3.0-video"},
            {"id": "gpt-image-1"},
        ]})
        with patch("app.main.httpx.AsyncClient", mock):
            response = client.post(
                "/admin/api/upstreams/models",
                headers={"X-CSRF-Token": csrf_token(session)},
                json={"base_url": "https://fuyao47.xyz", "api_key": "fuyao-key"},
            )
        self.assertEqual(response.status_code, 200)
        url, kwargs = captured["get"][0]
        self.assertEqual(url, "https://fuyao47.xyz/v1/models")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fuyao-key")
        models = {item["upstream_model"]: item for item in response.json()["models"]}
        self.assertEqual(set(models), {"grok-imagine-video-1.5", "wan3.0-video"})
        self.assertEqual(models["grok-imagine-video-1.5"]["protocol"], fuyao.PROTOCOL)
        self.assertEqual(models["grok-imagine-video-1.5"]["profile"], "fuyao-grok")
        self.assertEqual(models["wan3.0-video"]["profile"], "fuyao-wan3")

    def test_create_poll_and_download(self):
        public_model = self._save_route("grok-imagine-video-1.5", "fuyao-grok")
        task_id = f"task_{time.time_ns()}"
        captured: dict = {}
        mock = _mock_client(
            captured,
            post_json={"id": task_id, "object": "video", "status": "queued", "progress": 0},
            get_json={
                "id": task_id,
                "status": "completed",
                "progress": 100,
                "video_url": "https://tmp.example/expiring.mp4",
            },
        )
        with patch("app.proxy.httpx.AsyncClient", mock):
            created = asyncio.run(create_video({
                "model": public_model,
                "prompt": "主体向镜头走来",
                "seconds": 15,
                "resolution": "720p",
                "aspect_ratio": "16:9",
                "image_urls": ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"],
            }, None))
            fetched = asyncio.run(fetch_task(task_id))

        self.assertEqual(json.loads(created.body)["status"], "queued")
        url, kwargs = captured["post"]
        self.assertEqual(url, "https://fuyao47.xyz/v1/videos")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fuyao-secret")
        self.assertNotIn("Idempotency-Key", kwargs["headers"])
        self.assertEqual(kwargs["json"], {
            "model": "grok-imagine-video-1.5",
            "prompt": "主体向镜头走来",
            "seconds": 15,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "reference_images": [
                {"url": "https://cdn.example/1.jpg", "role": "reference_image"},
                {"url": "https://cdn.example/2.jpg", "role": "reference_image"},
            ],
        })
        self.assertEqual(captured["get"][0][0], f"https://fuyao47.xyz/v1/videos/{task_id}")
        self.assertEqual(json.loads(fetched.body)["status"], "completed")

        # /content answers with the file itself: it is streamed unchanged.
        _looked_up, downloaded = self._download_with_content_answer(
            task_id, httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"video")
        )
        self.assertEqual(downloaded["url"], f"https://fuyao47.xyz/v1/videos/{task_id}/content")
        self.assertEqual(downloaded["headers"]["Authorization"], "Bearer fuyao-secret")

    def _completed_task(self) -> str:
        public_model = self._save_route("wan3.0-video-prime", "fuyao-wan3")
        task_id = f"wan_{time.time_ns()}"
        mock = _mock_client(
            {},
            post_json={"id": task_id, "status": "queued"},
            get_json={"id": task_id, "status": "completed", "progress": 100},
        )
        with patch("app.proxy.httpx.AsyncClient", mock):
            asyncio.run(create_video({"model": public_model, "prompt": "p", "seconds": 5}, None))
            asyncio.run(fetch_task(task_id))
        return task_id

    def _download_with_content_answer(self, task_id: str, answer: httpx.Response):
        """Run a download where Fuyao's ``/content`` gives ``answer``."""
        looked_up: list = []

        class ContentClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def stream(self, method, url, **kwargs):
                looked_up.append((method, url, kwargs))

                class Ctx:
                    async def __aenter__(self_inner):
                        return answer

                    async def __aexit__(self_inner, *_args):
                        return None

                return Ctx()

        downloaded: dict = {}

        async def mock_stream(source_url, _request, headers=None, **kwargs):
            downloaded.update(url=source_url, headers=headers, kwargs=kwargs)
            return Response(content=b"video", media_type="video/mp4")

        request = httpx.Request("GET", f"https://media.yyapi.cloud/public/videos/{task_id}/content")
        with patch("app.proxy.httpx.AsyncClient", ContentClient), patch(
            "app.proxy.stream_upstream_content", new=mock_stream
        ):
            asyncio.run(stream_content(task_id, request))
        return looked_up, downloaded

    def test_json_content_answer_is_resolved_to_the_video_file(self):
        # The body Fuyao really returned from /content for a finished wan task.
        signed = (
            "https://dashscope-a717.oss-accelerate.aliyuncs.com/1d/76/20260930/c46670cb/"
            "89564464-metadata.mp4?Expires=1790816809&OSSAccessKeyId=AK&Signature=SIG%3D"
        )
        task_id = self._completed_task()
        answer = httpx.Response(200, json={
            "id": task_id,
            "object": "video",
            "status": "completed",
            "task_status": "SUCCESS",
            "status_url": f"https://fuyao47.xyz/v1/videos/{task_id}",
            "content_url": signed,
            "url": signed,
            "video_url": signed,
            "download_url": signed,
            "output": {"task_status": "SUCCEEDED", "video_url": signed},
        })
        looked_up, downloaded = self._download_with_content_answer(task_id, answer)

        self.assertEqual(looked_up[0][1], f"https://fuyao47.xyz/v1/videos/{task_id}/content")
        self.assertEqual(looked_up[0][2]["headers"]["Authorization"], "Bearer fuyao-secret")
        # The viewer gets the file, streamed from storage without our key.
        self.assertEqual(downloaded["url"], signed)
        self.assertNotIn("Authorization", downloaded["headers"])
        self.assertTrue(downloaded["kwargs"]["reject_json"])

    def test_real_video_content_answer_is_streamed_as_before(self):
        task_id = self._completed_task()
        answer = httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"\x00mp4")
        _looked_up, downloaded = self._download_with_content_answer(task_id, answer)
        self.assertEqual(downloaded["url"], f"https://fuyao47.xyz/v1/videos/{task_id}/content")
        self.assertEqual(downloaded["headers"]["Authorization"], "Bearer fuyao-secret")

    def test_json_content_answer_without_a_link_is_a_502(self):
        task_id = self._completed_task()
        answer = httpx.Response(200, json={"id": task_id, "status": "completed"})
        with self.assertRaises(HTTPException) as raised:
            self._download_with_content_answer(task_id, answer)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertNotIn("fuyao47", raised.exception.detail)

    def test_extract_video_link_ignores_relative_and_missing_values(self):
        self.assertIsNone(fuyao.extract_video_link({"url": "/v1/videos/x/content"}))
        self.assertIsNone(fuyao.extract_video_link(["not", "a", "dict"]))
        self.assertEqual(
            fuyao.extract_video_link({"output": {"video_url": "https://oss.example/v.mp4"}}),
            "https://oss.example/v.mp4",
        )

    def test_unserviceable_request_is_a_400_before_any_upstream_call(self):
        public_model = self._save_route(SD_MINI, "fuyao-sd2", image_count=9)
        captured: dict = {}
        with patch("app.proxy.httpx.AsyncClient", _mock_client(captured)):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(create_video({"model": public_model, "prompt": "p", "seconds": 5}, None))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("纯文生视频", raised.exception.detail)
        self.assertNotIn("post", captured)


if __name__ == "__main__":
    unittest.main()
