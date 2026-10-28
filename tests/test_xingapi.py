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
from app.channels import xingapi
from app.main import app, normalize_discovered_models
from app.model_profiles import capabilities_for
from app.proxy import create_video, fetch_task, stream_content
from app.security import SESSION_COOKIE, create_session, csrf_token


BASE_URL = "https://xingapi.top/v1"


def _save_route(upstream_model: str, durations: list[int] | None = None, **counts) -> str:
    public_model = f"xing-{time.time_ns()}"
    route = xingapi.suggest_route(upstream_model)
    database.save_upstream({
        "name": public_model,
        "base_url": BASE_URL,
        "api_key": "xing-secret",
        "enabled": True,
        "priority": 1,
        "routes": [{
            "model": public_model,
            "upstream_model": upstream_model,
            "protocol": xingapi.PROTOCOL,
            "profile": xingapi.PROFILE,
            "durations": durations if durations is not None else route["durations"],
            "resolutions": route["resolutions"],
            "image_count": counts.get("image_count", route["image_count"]),
            "video_count": counts.get("video_count", route["video_count"]),
            "audio_count": counts.get("audio_count", route["audio_count"]),
        }],
    })
    return public_model


class _MockClient:
    """Answers the create, poll and model-list calls; records what was sent."""

    def __init__(self, captured: list, task_id: str, poll_body: dict | None = None, poll_status: int = 200):
        self.captured = captured
        self.task_id = task_id
        self.poll_body = poll_body or {"id": task_id, "status": "succeeded", "progress": 100}
        self.poll_status = poll_status

    def __call__(self, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.captured.append(("POST", url, kwargs))
        return httpx.Response(200, request=httpx.Request("POST", url), json={
            "id": self.task_id, "object": "video", "status": "queued",
        })

    async def get(self, url, **kwargs):
        self.captured.append(("GET", url, kwargs))
        return httpx.Response(self.poll_status, request=httpx.Request("GET", url), json=self.poll_body)


class XingApiTransformTests(unittest.TestCase):
    def test_public_fields_only_and_canonical_aliases_are_mapped(self):
        payload = xingapi.transform_create_payload({
            "model": "seedance2.0-mini",
            "prompt": "以 @Image1 为人物参考",
            "seconds": "5",
            "aspect_ratio": "9:16",
            "metadata": {"resolution": "720P"},
            "image_url": "https://cdn.example/person.png",
            "image_urls": ["https://cdn.example/person.png", {"url": "https://cdn.example/second.png"}],
            "audio_urls": ["https://cdn.example/a.mp3"],
        })
        self.assertEqual(payload, {
            "model": "seedance2.0-mini",
            "prompt": "以 @Image1 为人物参考",
            "duration": 5,
            "ratio": "9:16",
            "resolution": "720p",
            "images": ["https://cdn.example/person.png", "https://cdn.example/second.png"],
            "audios": ["https://cdn.example/a.mp3"],
        })
        for leaked in ("seconds", "image_urls", "image_url", "aspect_ratio", "metadata", "audio_urls"):
            self.assertNotIn(leaked, payload)

    def test_size_supplies_ratio_and_resolution(self):
        payload = xingapi.transform_create_payload({
            "model": "seedance-2.0-933x", "prompt": "海浪", "duration": 5, "size": "1280x720",
        })
        self.assertEqual(payload["ratio"], "16:9")
        self.assertEqual(payload["resolution"], "720p")

    def test_duration_is_required_unless_the_route_has_one_length(self):
        with self.assertRaises(xingapi.XingApiRequestError):
            xingapi.transform_create_payload({"model": "seedance-2", "prompt": "x"}, list(range(4, 16)))
        payload = xingapi.transform_create_payload({"model": "快乐马1.1-10S", "prompt": "x"}, [10])
        self.assertEqual(payload["duration"], 10)

    def test_inline_media_is_rejected(self):
        with self.assertRaises(xingapi.XingApiRequestError):
            xingapi.transform_create_payload({
                "model": "seedance-2.0-933x", "prompt": "x", "duration": 5,
                "audios": ["data:audio/mp3;base64,AAAA"],
            })

    def test_routes_follow_the_documented_backends(self):
        pure900 = xingapi.suggest_route("seedance2.0-mini")
        self.assertEqual((pure900["image_count"], pure900["video_count"], pure900["audio_count"]), (10, 0, 3))
        miaowu = xingapi.suggest_route("seedance-2.0-933x")
        self.assertEqual(miaowu["video_count"], 0)
        self.assertEqual(miaowu["audio_count"], 3)
        self.assertEqual(xingapi.suggest_route("快乐马1.1-5S")["durations"], [5])
        self.assertEqual(xingapi.suggest_route("Seedance2.0")["durations"], list(xingapi.DURATIONS))
        caps = capabilities_for(xingapi.PROFILE, pure900["durations"], 10, 0, 3, pure900["resolutions"])
        self.assertEqual(caps["ratios"], ["16:9", "9:16"])
        self.assertFalse(caps["referenceVideo"])
        self.assertEqual(caps["maxAudios"], 3)

    def test_task_fields_and_status_aliases(self):
        self.assertEqual(xingapi.api_url(BASE_URL, "/v1/videos/a"), "https://xingapi.top/v1/videos/a")
        self.assertEqual(xingapi.api_url("https://xingapi.top", "/v1/videos/a"), "https://xingapi.top/v1/videos/a")
        self.assertEqual(xingapi.content_path("task/x"), "/v1/videos/task%2Fx/content")
        self.assertEqual(xingapi.extract_create_task_id({"task_id": "t1", "id": "t2"}), "t1")
        self.assertEqual(xingapi.extract_create_task_id({"data": {"id": "t3", "status": "queued"}}), "t3")
        for status, expected in (
            ("in_progress", "processing"), ("finished", "completed"), ("done", "completed"),
            ("canceled", "failed"), ("error", "failed"), ("pending", "queued"),
        ):
            self.assertEqual(xingapi.extract_task_fields({"status": status})["status"], expected)
        fields = xingapi.extract_task_fields({"status": "failed", "error": {"message": "bad prompt"}, "progress": "40%"})
        self.assertEqual(fields["error"], {"message": "bad prompt"})
        self.assertEqual(fields["progress"], "40")
        self.assertIsNone(xingapi.extract_task_fields({"status": "success", "video_url": "https://x"})["video_url"])

    def test_discovery_keeps_video_models_only(self):
        routes = normalize_discovered_models(
            {"data": [{"id": "seedance2.0-mini"}, {"id": "快乐马1.1-5S"}, {"id": "gpt-4o-mini"}]},
            xingapi.PROTOCOL,
        )
        self.assertEqual([route["upstream_model"] for route in routes], ["seedance2.0-mini", "快乐马1.1-5S"])
        self.assertTrue(all(route["protocol"] == xingapi.PROTOCOL for route in routes))


class XingApiProxyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()

    def test_create_poll_and_download_use_the_documented_endpoints(self):
        public_model = _save_route("seedance2.0-mini")
        task_id = f"task_xing_{time.time_ns()}"
        captured: list = []
        client = _MockClient(captured, task_id)
        with patch("app.proxy.httpx.AsyncClient", client):
            created = asyncio.run(create_video({
                "model": public_model,
                "prompt": "以 @Image1 为人物参考，自然说话",
                "seconds": 5,
                "aspect_ratio": "9:16",
                "image_urls": ["https://cdn.example/person.png"],
                "audio_urls": ["https://cdn.example/1.mp3", "https://cdn.example/2.mp3"],
            }, None))
            fetched = asyncio.run(fetch_task(task_id))

        self.assertEqual(created.status_code, 200)
        self.assertEqual(json.loads(created.body)["id"], task_id)
        method, url, kwargs = captured[0]
        self.assertEqual((method, url), ("POST", "https://xingapi.top/v1/videos/generations"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer xing-secret")
        self.assertEqual(kwargs["json"], {
            "model": "seedance2.0-mini",
            "prompt": "以 @Image1 为人物参考，自然说话",
            "duration": 5,
            "ratio": "9:16",
            "images": ["https://cdn.example/person.png"],
            "audios": ["https://cdn.example/1.mp3", "https://cdn.example/2.mp3"],
        })
        self.assertEqual(captured[1][1], f"https://xingapi.top/v1/videos/{task_id}")
        self.assertEqual(json.loads(fetched.body)["status"], "completed")

        download: dict = {}

        async def mock_stream(source_url, _request, headers=None, **_kwargs):
            download.update(url=source_url, headers=headers)
            return Response(content=b"video", media_type="video/mp4")

        request = httpx.Request("GET", f"https://relay.example/v1/videos/{task_id}/content")
        with patch("app.proxy.stream_upstream_content", new=mock_stream):
            asyncio.run(stream_content(task_id, request))
        self.assertEqual(download["url"], f"https://xingapi.top/v1/videos/{task_id}/content")
        self.assertEqual(download["headers"]["Authorization"], "Bearer xing-secret")

    def test_missing_duration_is_rejected_before_anything_is_sent(self):
        public_model = _save_route("seedance-2.0-933x")
        captured: list = []
        with patch("app.proxy.httpx.AsyncClient", _MockClient(captured, "unused")):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(create_video({"model": public_model, "prompt": "x"}, None))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(captured, [])

    def test_reference_video_is_refused_on_these_backends(self):
        public_model = _save_route("seedance-2.0-933x")
        captured: list = []
        with patch("app.proxy.httpx.AsyncClient", _MockClient(captured, "unused")):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(create_video({
                    "model": public_model, "prompt": "x", "duration": 5,
                    "videos": ["https://cdn.example/ref.mp4"],
                }, None))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(captured, [])

    def test_fixed_length_route_fills_in_its_duration(self):
        public_model = _save_route("快乐马1.1-10S")
        task_id = f"task_xing_{time.time_ns()}"
        captured: list = []
        with patch("app.proxy.httpx.AsyncClient", _MockClient(captured, task_id)):
            asyncio.run(create_video({"model": public_model, "prompt": "马在草原奔跑"}, None))
        self.assertEqual(captured[0][2]["json"]["duration"], 10)

    def test_rejected_key_on_poll_keeps_the_task_pending(self):
        public_model = _save_route("seedance-2")
        task_id = f"task_xing_{time.time_ns()}"
        captured: list = []
        with patch("app.proxy.httpx.AsyncClient", _MockClient(captured, task_id)):
            asyncio.run(create_video({"model": public_model, "prompt": "x", "duration": 5}, None))
        rejected = _MockClient(captured, task_id, {"error": {"message": "invalid key"}}, poll_status=401)
        with patch("app.proxy.httpx.AsyncClient", rejected):
            asyncio.run(fetch_task(task_id))
        self.assertNotEqual(database.get_task(task_id)["status"], "failed")

    def test_failed_task_reports_the_upstream_reason(self):
        public_model = _save_route("seedance-2")
        task_id = f"task_xing_{time.time_ns()}"
        captured: list = []
        failed = _MockClient(captured, task_id, {"id": task_id, "status": "failed", "error": {"message": "审核未通过"}})
        with patch("app.proxy.httpx.AsyncClient", failed):
            asyncio.run(create_video({"model": public_model, "prompt": "x", "duration": 5}, None))
            fetched = json.loads(asyncio.run(fetch_task(task_id)).body)
        self.assertEqual(fetched["status"], "failed")
        self.assertEqual(fetched["error"]["message"], "审核未通过")

    def test_model_sync_probes_the_v1_models_endpoint_with_the_key(self):
        captured: list = []
        mock = _MockClient(captured, "unused", {"data": [{"id": "seedance2.0-mini"}, {"id": "gpt-4o"}]})
        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        with patch("app.main.httpx.AsyncClient", mock):
            response = client.post(
                "/admin/api/upstreams/models",
                headers={"X-CSRF-Token": csrf_token(session)},
                json={"base_url": BASE_URL, "api_key": "xing-key"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured[0][1], "https://xingapi.top/v1/models")
        self.assertEqual(captured[0][2]["headers"]["Authorization"], "Bearer xing-key")
        models = response.json()["models"]
        self.assertEqual([item["upstream_model"] for item in models], ["seedance2.0-mini"])
        self.assertEqual(models[0]["protocol"], xingapi.PROTOCOL)
        self.assertEqual(models[0]["profile"], xingapi.PROFILE)


if __name__ == "__main__":
    unittest.main()
