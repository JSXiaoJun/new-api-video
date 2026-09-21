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

from fastapi.responses import Response

from app import database
from app.channels import mai_token
from app.model_profiles import capabilities_for, suggest_protocol
from app.main import app, normalize_discovered_models
from app.proxy import create_video, fetch_task, stream_content
from app.security import SESSION_COOKIE, create_session, csrf_token
from fastapi.testclient import TestClient


class MaiTokenAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()

    def test_base_url_detection_is_isolated(self):
        self.assertTrue(mai_token.is_mai_token_base_url("https://api.mai-token.com"))
        self.assertTrue(mai_token.is_mai_token_base_url("https://api.mai-token.com/v1"))
        self.assertFalse(mai_token.is_mai_token_base_url("https://mai-token.com"))
        self.assertFalse(mai_token.is_mai_token_base_url("https://api.pro666.top"))

    def test_documented_models_map_to_their_resolution_profiles(self):
        # A model nobody hardcoded still routes by its resolution suffix, so a
        # tier published upstream after this adapter shipped keeps working.
        self.assertEqual(mai_token.suggest_route("sd-3.0-720p")["profile"], "mai-token-720p")
        self.assertEqual(mai_token.suggest_route("sd-3.0-1080p")["resolutions"], ["1080p"])
        # A name with no resolution suffix cannot be routed and must be refused
        # rather than guessed at.
        self.assertIsNone(mai_token.suggest_route("sd-3.0"))
        self.assertIsNone(mai_token.suggest_route(""))
        expected = {
            "sd-2.0-1080p": ("mai-token-1080p", "1080p"),
            "sd-2.0-720p": ("mai-token-720p", "720p"),
            "sd-2.0-480p": ("mai-token-480p", "480p"),
            "sd-fast-720p": ("mai-token-720p", "720p"),
            "sd-fast-480p": ("mai-token-480p", "480p"),
            "sd-mini-720p": ("mai-token-720p", "720p"),
            "sd-mini-480p": ("mai-token-480p", "480p"),
        }
        for model, (profile, resolution) in expected.items():
            route = mai_token.suggest_route(model)
            self.assertIsNotNone(route, model)
            self.assertEqual(route["profile"], profile)
            self.assertEqual(route["resolutions"], [resolution])
            self.assertEqual(route["durations"], list(range(4, 16)))

    def test_protocol_detection_stays_strict(self):
        # ``suggest_route`` is intentionally loose, so detection must not use
        # it: another channel's model would otherwise be captured here.
        self.assertTrue(mai_token.is_known_model("sd-2.0-720p"))
        self.assertFalse(mai_token.is_known_model("sd-3.0-720p"))
        self.assertFalse(mai_token.is_known_model("v1-seedance-2.0-720p"))
        self.assertEqual(suggest_protocol("v1-seedance-2.0-720p"), "videos")
        self.assertEqual(suggest_protocol("sd-2.0-720p"), mai_token.PROTOCOL)

    def test_discovered_routes_keep_resolution_isolated_per_model(self):
        routes = normalize_discovered_models(
            list(mai_token.KNOWN_MODELS), mai_token.PROTOCOL
        )
        by_model = {route["upstream_model"]: route for route in routes}
        self.assertEqual(by_model["sd-2.0-1080p"]["protocol"], mai_token.PROTOCOL)
        self.assertEqual(by_model["sd-2.0-1080p"]["resolutions"], ["1080p"])
        self.assertEqual(by_model["sd-2.0-720p"]["resolutions"], ["720p"])
        self.assertEqual(by_model["sd-mini-480p"]["resolutions"], ["480p"])

    def test_model_discovery_probes_the_upstream_and_returns_live_models(self):
        # The catalog must come from the upstream, so a model published after
        # this adapter shipped shows up in the console without a code change.
        captured = []

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def get(self, url, **kwargs):
                captured.append((url, kwargs.get("headers", {})))
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", url),
                    json={"data": [{"id": "sd-2.0-720p"}, {"id": "sd-4.0-720p"}]},
                )

        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        with patch("app.main.httpx.AsyncClient", MockAsyncClient):
            response = client.post(
                "/admin/api/upstreams/models",
                headers={"X-CSRF-Token": csrf_token(session)},
                json={"base_url": "https://api.mai-token.com", "api_key": "mai-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([url for url, _ in captured], ["https://api.mai-token.com/v1/models"])
        # The probe has to carry the channel key, otherwise the upstream
        # answers 401 and the console cannot tell models from an auth failure.
        self.assertEqual(captured[0][1]["Authorization"], "Bearer mai-key")
        models = {item["upstream_model"]: item for item in response.json()["models"]}
        self.assertIn("sd-4.0-720p", models)
        self.assertEqual(models["sd-4.0-720p"]["protocol"], mai_token.PROTOCOL)
        self.assertEqual(models["sd-4.0-720p"]["profile"], "mai-token-720p")

    def test_model_discovery_falls_back_to_documented_models_when_endpoint_is_absent(self):
        # A 404 proves the endpoint does not exist, so the documented catalog is
        # the only source left and discovery must still succeed.
        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def get(self, url, **_kwargs):
                return httpx.Response(404, request=httpx.Request("GET", url), json={"error": "not found"})

        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        with patch("app.main.httpx.AsyncClient", MockAsyncClient):
            response = client.post(
                "/admin/api/upstreams/models",
                headers={"X-CSRF-Token": csrf_token(session)},
                json={"base_url": "https://api.mai-token.com", "api_key": "mai-key"},
            )

        self.assertEqual(response.status_code, 200)
        models = {item["upstream_model"] for item in response.json()["models"]}
        self.assertEqual(models, set(mai_token.KNOWN_MODELS))

    def test_model_discovery_surfaces_an_auth_failure_instead_of_a_local_list(self):
        # A 401 is a real failure. Answering it with the local catalog would
        # hide a bad key behind a plausible-looking model list.
        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def get(self, url, **_kwargs):
                return httpx.Response(401, request=httpx.Request("GET", url), json={"error": "bad key"})

        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        with patch("app.main.httpx.AsyncClient", MockAsyncClient):
            response = client.post(
                "/admin/api/upstreams/models",
                headers={"X-CSRF-Token": csrf_token(session)},
                json={"base_url": "https://api.mai-token.com", "api_key": "wrong-key"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("401", response.json()["detail"])

    def test_capabilities_match_the_documented_limits(self):
        caps = capabilities_for("mai-token-720p")
        self.assertEqual(caps["ratios"], ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"])
        self.assertEqual(caps["durations"], list(range(4, 16)))
        self.assertEqual(caps["maxImages"], 9)
        self.assertEqual(caps["maxAudios"], 3)
        self.assertEqual(caps["maxReferences"], 15)
        self.assertTrue(caps["referenceVideo"])

    def test_text_request_duplicates_prompt_and_omits_conflicting_resolution(self):
        payload = mai_token.transform_create_payload({
            "model": "sd-2.0-720p",
            "prompt": "夜晚天台的动作打斗",
            "seconds": "5",
            "ratio": "16:9",
            "generate_audio": True,
            "resolution": "1080p",
            "size": "1920x1080",
            "seed": -1,
            "extra_field": "dropped",
        })
        self.assertEqual(payload, {
            "model": "sd-2.0-720p",
            "prompt": "夜晚天台的动作打斗",
            "content": [{"type": "text", "text": "夜晚天台的动作打斗"}],
            "seconds": "5",
            "ratio": "16:9",
            "generate_audio": True,
            "seed": -1,
        })
        self.assertNotIn("resolution", payload)
        self.assertNotIn("size", payload)
        self.assertNotIn("extra_field", payload)

    def test_media_is_split_into_typed_content_items_with_documented_caps(self):
        payload = mai_token.transform_create_payload({
            "model": "sd-2.0-720p",
            "prompt": "@image1 参考 @video1 与 @audio1",
            "duration": 15,
            "image_urls": [
                {"url": f"https://cdn.example/img-{index}.png"} for index in range(12)
            ],
            "reference_image_urls": ["https://cdn.example/extra.png"],
            "audio_urls": [f"https://cdn.example/audio-{index}.mp3" for index in range(5)],
            "reference_videos": [f"https://cdn.example/video-{index}.mp4" for index in range(5)],
        })
        content = payload["content"]
        self.assertEqual(content[0], {"type": "text", "text": "@image1 参考 @video1 与 @audio1"})
        images = [item for item in content if item["type"] == "image_url"]
        audios = [item for item in content if item["type"] == "audio_url"]
        videos = [item for item in content if item["type"] == "video_url"]
        self.assertEqual(len(images), mai_token.MAX_IMAGES)
        self.assertEqual(len(audios), mai_token.MAX_AUDIOS)
        self.assertEqual(len(videos), mai_token.MAX_VIDEOS)
        self.assertTrue(all(item["role"] == "reference_image" for item in images))
        self.assertTrue(all(item["role"] == "reference_audio" for item in audios))
        self.assertTrue(all(item["role"] == "reference_video" for item in videos))
        self.assertEqual(payload["seconds"], "15")

    def test_first_and_last_frames_use_roles_and_share_the_image_budget(self):
        payload = mai_token.transform_create_payload({
            "model": "sd-2.0-720p",
            "prompt": "从首帧过渡到尾帧",
            "first_frame_url": "https://cdn.example/first.png",
            "last_frame": {"url": "https://cdn.example/last.png"},
            "image_urls": [f"https://cdn.example/ref-{index}.png" for index in range(9)],
        })
        images = [item for item in payload["content"] if item["type"] == "image_url"]
        self.assertEqual(images[0]["role"], "first_frame")
        self.assertEqual(images[0]["image_url"]["url"], "https://cdn.example/first.png")
        self.assertEqual(images[1]["role"], "last_frame")
        self.assertEqual(images[1]["image_url"]["url"], "https://cdn.example/last.png")
        self.assertEqual(len(images), mai_token.MAX_IMAGES)

    def test_ratio_is_derived_from_size_and_task_paths_are_encoded(self):
        payload = mai_token.transform_create_payload({
            "model": "sd-fast-480p",
            "prompt": "海浪",
            "size": "1920x1080",
        })
        self.assertEqual(payload["ratio"], "16:9")
        self.assertNotIn("seconds", payload)
        self.assertEqual(mai_token.task_path("task_abc"), "/v1/videos/task_abc")
        self.assertEqual(mai_token.content_path("task/a"), "/v1/videos/task%2Fa/content")
        self.assertEqual(
            mai_token.extract_create_task_id({"id": "task_1", "task_id": "ignored"}), "task_1"
        )

    def test_task_fields_accept_documented_poll_envelope(self):
        fields = mai_token.extract_task_fields({
            "status": "in_progress",
            "progress": 56,
            "error": None,
        })
        self.assertEqual(fields["status"], "in_progress")
        self.assertEqual(fields["progress"], 56)
        self.assertIsNone(fields["video_url"])
        completed = mai_token.extract_task_fields({
            "status": "completed",
            "progress": 100,
            "video_url": "https://cdn.mai-token.com/video/task_xxx.mp4",
        })
        self.assertEqual(completed["video_url"], "https://cdn.mai-token.com/video/task_xxx.mp4")
        failed = mai_token.extract_task_fields({
            "status": "failed",
            "error": {"code": "generation_failed", "message": "video generation failed"},
        })
        self.assertEqual(failed["error"]["message"], "video generation failed")

    def test_proxy_create_poll_and_download_are_channel_isolated(self):
        public_model = f"mai-token-public-{time.time_ns()}"
        upstream = database.save_upstream({
            "name": public_model,
            "base_url": "https://api.mai-token.com",
            "api_key": "mai-secret",
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": public_model,
                "upstream_model": "sd-2.0-720p",
                "protocol": mai_token.PROTOCOL,
                "profile": "mai-token-720p",
                "durations": list(range(4, 16)),
                "resolutions": ["720p"],
                "image_count": 9,
                "supports_image": True,
                "supports_video": True,
                "supports_audio": True,
            }],
        })
        self.assertEqual(upstream["routes"][0]["protocol"], mai_token.PROTOCOL)
        captured: dict[str, tuple[str, dict]] = {}
        task_id = f"task_{time.time_ns()}"
        video_url = "https://cdn.mai-token.com/video/result.mp4"

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
                    "id": task_id, "object": "video", "status": "queued", "progress": 0,
                })

            async def get(self, url, **kwargs):
                captured["get"] = (url, kwargs)
                return httpx.Response(200, request=httpx.Request("GET", url), json={
                    "id": task_id, "status": "completed", "progress": 100, "video_url": video_url,
                })

        with patch("app.proxy.httpx.AsyncClient", MockAsyncClient):
            created = asyncio.run(create_video({
                "model": public_model,
                "prompt": "让 @image1 的人物做出武术动作",
                "seconds": "5",
                "ratio": "16:9",
                "resolution": "1080p",
                "generate_audio": True,
                "image_urls": ["https://cdn.example/person.jpg"],
            }, None))
            fetched = asyncio.run(fetch_task(task_id))

        self.assertEqual(created.status_code, 200)
        self.assertEqual(json.loads(created.body)["status"], "queued")
        self.assertEqual(captured["post"][0], "https://api.mai-token.com/v1/videos")
        self.assertEqual(captured["post"][1]["headers"]["Authorization"], "Bearer mai-secret")
        self.assertEqual(captured["post"][1]["json"], {
            "model": "sd-2.0-720p",
            "prompt": "让 @image1 的人物做出武术动作",
            "content": [
                {"type": "text", "text": "让 @image1 的人物做出武术动作"},
                {
                    "type": "image_url",
                    "role": "reference_image",
                    "image_url": {"url": "https://cdn.example/person.jpg"},
                },
            ],
            "seconds": "5",
            "ratio": "16:9",
            "generate_audio": True,
        })
        self.assertEqual(captured["get"][0], f"https://api.mai-token.com/v1/videos/{task_id}")
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

    def test_completed_task_without_video_url_falls_back_to_content_endpoint(self):
        public_model = f"mai-token-download-{time.time_ns()}"
        database.save_upstream({
            "name": public_model,
            "base_url": "https://api.mai-token.com",
            "api_key": "mai-secret",
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": public_model,
                "upstream_model": "sd-fast-480p",
                "protocol": mai_token.PROTOCOL,
                "profile": "mai-token-480p",
                "durations": [4],
                "resolutions": ["480p"],
                "image_count": 0,
                "supports_image": False,
                "supports_video": False,
                "supports_audio": False,
            }],
        })
        task_id = f"task_{time.time_ns()}"
        database.create_task(task_id, database.select_upstream(public_model)["id"], None, public_model, mai_token.PROTOCOL, "completed")
        database.update_task(task_id, "completed", None, None)

        captured_download: dict[str, object] = {}

        async def mock_stream(source_url, _request, headers=None, **_kwargs):
            captured_download["url"] = source_url
            return Response(content=b"video", media_type="video/mp4")

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, url, **_kwargs):
                return httpx.Response(200, request=httpx.Request("GET", url), json={
                    "id": task_id, "status": "completed", "progress": 100,
                })

        request = httpx.Request("GET", f"https://media.yyapi.cloud/public/videos/{task_id}/content")
        with patch("app.proxy.httpx.AsyncClient", MockAsyncClient), patch(
            "app.proxy.stream_upstream_content", new=mock_stream
        ):
            asyncio.run(stream_content(task_id, request))
        self.assertEqual(
            captured_download["url"],
            f"https://api.mai-token.com/v1/videos/{task_id}/content",
        )


if __name__ == "__main__":
    unittest.main()
