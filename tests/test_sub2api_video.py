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

from app import database
from app.channels import o10_grok, sub2api_video
from app.main import normalize_discovered_models
from app.proxy import create_video, fetch_task, stream_content
from app.model_profiles import capabilities_for
from fastapi.responses import Response


class Sub2ApiVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()

    def test_known_models_and_capabilities_are_isolated(self):
        self.assertTrue(sub2api_video.is_sub2api_base_url("https://api.pandatk.com"))
        self.assertTrue(sub2api_video.is_sub2api_base_url("https://api.pandatk.com/v1"))
        self.assertFalse(sub2api_video.is_sub2api_base_url("https://o10.top"))

        routes = normalize_discovered_models(
            list(sub2api_video.KNOWN_MODELS), sub2api_video.PROTOCOL
        )
        by_model = {route["upstream_model"]: route for route in routes}
        text = by_model["grok-imagine-video"]
        image = by_model["grok-imagine-video-1.5"]
        self.assertEqual(text["profile"], sub2api_video.PROFILE)
        self.assertEqual(text["image_count"], 0)
        self.assertEqual(text["resolutions"], ["480p", "720p"])
        self.assertEqual(image["image_count"], 7)
        self.assertEqual(image["resolutions"], ["480p", "720p", "1080p"])
        caps = capabilities_for(
            image["profile"], image["durations"], image["supports_image"],
            image["supports_video"], image["supports_audio"], image["image_count"],
            image["resolutions"],
        )
        self.assertEqual(caps["ratios"], ["16:9", "9:16", "1:1", "4:3", "3:4", "2:3", "3:2"])
        self.assertEqual(caps["maxImages"], 7)
        self.assertFalse(caps["referenceVideo"])
        self.assertEqual(caps["maxAudios"], 0)

    def test_image_model_uses_documented_images_array_not_o10_image_object(self):
        payload = sub2api_video.transform_create_payload({
            "model": "grok-imagine-video-1.5",
            "prompt": "人物对镜头说话",
            "seconds": 6,
            "metadata": {"ratio": "9:16", "resolution": "1080p"},
            "image": {"url": "https://cdn.example/main.png"},
            "reference_image_urls": ["https://cdn.example/second.png"],
            "images": [
                {"url": "https://cdn.example/third.png"},
                *[f"https://cdn.example/{index}.png" for index in range(4, 12)],
            ],
            "reference_video": "https://cdn.example/ignored.mp4",
            "audio_urls": ["https://cdn.example/ignored.mp3"],
        })
        self.assertEqual(payload, {
            "model": "grok-imagine-video-1.5",
            "prompt": "人物对镜头说话",
            "duration": 6,
            "ratio": "9:16",
            "size": "1080p",
            "images": [
                "https://cdn.example/main.png",
                "https://cdn.example/third.png",
                "https://cdn.example/4.png",
                "https://cdn.example/5.png",
                "https://cdn.example/6.png",
                "https://cdn.example/7.png",
                "https://cdn.example/8.png",
            ],
        })
        self.assertNotIn("image", payload)
        self.assertNotIn("aspect_ratio", payload)
        self.assertNotIn("resolution", payload)

    def test_text_model_drops_reference_images_and_old_o10_adapter_is_unchanged(self):
        sub2_payload = sub2api_video.transform_create_payload({
            "model": "grok-imagine-video",
            "prompt": "海浪",
            "duration": 1,
            "ratio": "16:9",
            "size": "720p",
            "image_urls": ["https://cdn.example/ignored.png"],
        })
        self.assertEqual(sub2_payload, {
            "model": "grok-imagine-video",
            "prompt": "海浪",
            "duration": 1,
            "ratio": "16:9",
            "size": "720p",
        })
        self.assertEqual(o10_grok.transform_create_payload({
            "model": "grok-imagine-video-1.5",
            "prompt": "旧上游保持不变",
            "image_urls": ["https://cdn.example/ref.png"],
        })["image"], {"url": "https://cdn.example/ref.png"})

    def test_task_fields_and_paths(self):
        self.assertEqual(sub2api_video.task_path("task/a"), "/v1/videos/task%2Fa")
        self.assertEqual(sub2api_video.content_path("task/a"), "/v1/videos/task%2Fa/content")
        self.assertEqual(sub2api_video.extract_create_task_id({"id": "task-1"}), "task-1")
        fields = sub2api_video.extract_task_fields({
            "status": "completed", "progress": 100, "url": "https://media.example/video.mp4",
        })
        self.assertEqual(fields["video_url"], "https://media.example/video.mp4")

    def test_proxy_uses_sub2api_endpoint_images_and_result_url(self):
        public_model = f"sub2api-public-{time.time_ns()}"
        upstream = database.save_upstream({
            "name": public_model,
            "base_url": "https://api.pandatk.com",
            "api_key": "sub2api-secret",
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": public_model,
                "upstream_model": "grok-imagine-video-1.5",
                "protocol": sub2api_video.PROTOCOL,
                "profile": sub2api_video.PROFILE,
                "durations": [1, 15],
                "resolutions": ["480p", "720p", "1080p"],
                "image_count": 7,
                "supports_image": True,
                "supports_video": False,
                "supports_audio": False,
            }],
        })
        captured: dict[str, tuple[str, dict]] = {}
        task_id = f"sub2api-task-{time.time_ns()}"
        video_url = "https://media.sub2api.example/result.mp4?signature=temporary"

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                captured["post"] = (url, kwargs)
                return httpx.Response(200, request=httpx.Request("POST", url), json={
                    "id": task_id, "task_id": task_id, "status": "queued", "progress": 0,
                })

            async def get(self, url, **kwargs):
                captured["get"] = (url, kwargs)
                return httpx.Response(200, request=httpx.Request("GET", url), json={
                    "id": task_id, "status": "completed", "progress": 100, "video_url": video_url,
                })

        with patch("app.proxy.httpx.AsyncClient", MockAsyncClient):
            created = asyncio.run(create_video({
                "model": public_model,
                "prompt": "保持人物一致并说话",
                "seconds": 6,
                "aspect_ratio": "9:16",
                "resolution": "1080p",
                "image_urls": ["https://cdn.example/main.png", "https://cdn.example/second.png"],
            }, None))
            fetched = asyncio.run(fetch_task(task_id))

        self.assertEqual(created.status_code, 200)
        self.assertEqual(json.loads(created.body)["status"], "queued")
        self.assertEqual(captured["post"][0], "https://api.pandatk.com/v1/videos")
        self.assertEqual(captured["post"][1]["headers"]["Authorization"], "Bearer sub2api-secret")
        self.assertEqual(captured["post"][1]["json"], {
            "model": "grok-imagine-video-1.5",
            "prompt": "保持人物一致并说话",
            "duration": 6,
            "ratio": "9:16",
            "size": "1080p",
            "images": ["https://cdn.example/main.png", "https://cdn.example/second.png"],
        })
        self.assertNotIn("image", captured["post"][1]["json"])
        self.assertEqual(captured["get"][0], f"https://api.pandatk.com/v1/videos/{task_id}")
        self.assertEqual(json.loads(fetched.body)["status"], "completed")
        self.assertEqual(database.get_task(task_id)["source_video_url"], video_url)

        captured_download: dict[str, object] = {}

        async def mock_stream(source_url, _request, headers=None, **_kwargs):
            captured_download["url"] = source_url
            captured_download["headers"] = headers
            return Response(content=b"video", media_type="video/mp4")

        request = httpx.Request("GET", f"https://media.yyapi.cloud/public/videos/{task_id}/content")
        with patch("app.proxy.stream_upstream_content", new=mock_stream):
            asyncio.run(stream_content(task_id, request))
        self.assertEqual(captured_download["url"], video_url)
        self.assertNotIn("Authorization", captured_download["headers"])
        self.assertEqual(upstream["routes"][0]["protocol"], sub2api_video.PROTOCOL)


if __name__ == "__main__":
    unittest.main()
