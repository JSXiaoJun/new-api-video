from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-with-more-than-32-chars")
os.environ.setdefault("ADAPTER_API_KEY", "test-adapter-key")
os.environ.setdefault("ENCRYPTION_KEY", "IougsRYbjtzQcNSrzLV2O-TQ3k1PDP69XcfdR3Lxp3I=")
os.environ.setdefault("NEW_API_PUBLIC_BASE_URL", "https://zl.yyapi.cloud")
os.environ.setdefault("PUBLIC_BASE_URL", "https://video-admin.yyapi.cloud")
TEST_DATA_DIR = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = TEST_DATA_DIR.name

from app import database, image_database
from app.config import settings
from app.main import app, normalize_discovered_models
from app.image_proxy import classify_health_outcome, forward_json
from app.model_profiles import capabilities_for, transform_create_payload
from app.proxy import create_video, fetch_task, normalize_status, normalize_task_payload, stream_content, upstream_error
from app.security import SESSION_COOKIE, create_session, csrf_token, read_session, secret_box
from fastapi.responses import Response
from fastapi.testclient import TestClient


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()
        image_database.initialize()
        cls.upstream = database.save_upstream(
            {
                "name": "audit-upstream",
                "base_url": "https://private-upstream.example",
                "api_key": "private-key",
                "enabled": True,
                "priority": 1,
                "routes": [
                    {"model": "audit-model", "protocol": "videos"},
                    {
                        "model": "stable-manxue",
                        "upstream_model": "manxue-900-10s",
                        "protocol": "videos",
                        "profile": "manxue-933",
                        "duration_override": 10,
                    },
                ],
            }
        )

    def test_admin_page_redirects_to_login_without_session(self):
        response = TestClient(app).get("/admin", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/admin/login")

    def test_admin_page_has_copy_all_audit_control(self):
        client = TestClient(app)
        client.cookies.set(SESSION_COOKIE, create_session("admin"))
        response = client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="copy-audit-json"', response.text)
        self.assertIn("请求格式", response.text)
        self.assertIn("转换后参数", response.text)

    def test_image_admin_page_and_api_require_admin_session(self):
        client = TestClient(app)
        self.assertEqual(client.get("/admin/images", follow_redirects=False).status_code, 302)
        self.assertEqual(client.get("/admin/api/images/dashboard").status_code, 401)
        client.cookies.set(SESSION_COOKIE, create_session("admin"))
        response = client.get("/admin/images")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="image-upstream-rows"', response.text)

    def test_image_router_prefers_health_adjusted_low_cost_route(self):
        model = f"image-route-{time.time_ns()}"
        cheap = image_database.save_upstream(
            {
                "name": "cheap-image",
                "base_url": "https://cheap-image.example",
                "api_key": "cheap-key",
                "enabled": True,
                "priority": 1,
                "routes": [{
                    "public_model": model,
                    "upstream_model": "cheap-native",
                    "sizes": ["1k"],
                    "qualities": ["medium"],
                    "operations": ["generation"],
                    "cost_per_request": 0.04,
                }],
            }
        )
        image_database.save_upstream(
            {
                "name": "reliable-image",
                "base_url": "https://reliable-image.example",
                "api_key": "reliable-key",
                "enabled": True,
                "priority": 1,
                "routes": [{
                    "public_model": model,
                    "upstream_model": "reliable-native",
                    "sizes": ["1k"],
                    "qualities": ["medium"],
                    "operations": ["generation"],
                    "cost_per_request": 0.11,
                }],
            }
        )
        selected = image_database.select_route(model, "1024x1024", "medium", "generation")
        self.assertEqual(selected["upstream_id"], cheap["id"])
        for _ in range(3):
            image_database.record_request(
                selected, "generation", model, "1k", "medium", False, 503, 10, "failure"
            )
        self.assertEqual(
            image_database.select_route(model, "1024x1024", "medium", "generation")["upstream_model"],
            "reliable-native",
        )
        self.assertEqual(classify_health_outcome(400, "内容审核未通过"), "neutral")
        self.assertEqual(classify_health_outcome(401), "failure")
        self.assertEqual(classify_health_outcome(402), "failure")
        self.assertEqual(classify_health_outcome(403, "content policy rejected"), "neutral")
        self.assertEqual(classify_health_outcome(403, "service unavailable"), "failure")
        self.assertEqual(classify_health_outcome(503), "failure")

    def test_image_router_tracks_generation_and_edit_health_separately(self):
        model = f"image-operation-health-{time.time_ns()}"
        preferred = image_database.save_upstream(
            {
                "name": "preferred-both-operations",
                "base_url": "https://preferred-both.example",
                "api_key": "preferred-key",
                "enabled": True,
                "priority": 1,
                "routes": [{
                    "public_model": model,
                    "upstream_model": "preferred-native",
                    "sizes": ["1k"],
                    "qualities": ["medium"],
                    "operations": ["generation", "edit"],
                    "cost_per_request": 0.04,
                }],
            }
        )
        image_database.save_upstream(
            {
                "name": "generation-fallback",
                "base_url": "https://generation-fallback.example",
                "api_key": "fallback-key",
                "enabled": True,
                "priority": 1,
                "routes": [{
                    "public_model": model,
                    "upstream_model": "fallback-native",
                    "sizes": ["1k"],
                    "qualities": ["medium"],
                    "operations": ["generation"],
                    "cost_per_request": 0.11,
                }],
            }
        )

        selected = image_database.select_route(model, "1024x1024", "medium", "edit")
        self.assertEqual(selected["upstream_id"], preferred["id"])
        for _ in range(3):
            image_database.record_request(
                selected, "edit", model, "1k", "medium", False, 503, 10, "failure"
            )

        generation_route = image_database.select_route(
            model, "1024x1024", "medium", "generation"
        )
        self.assertEqual(generation_route["upstream_model"], "preferred-native")

    def test_image_proxy_rewrites_model_and_anonymizes_url(self):
        model = f"image-proxy-{time.time_ns()}"
        image_database.save_upstream(
            {
                "name": "proxy-image",
                "base_url": "https://proxy-image.example/v1",
                "api_key": "proxy-key",
                "enabled": True,
                "priority": 1,
                "routes": [{
                    "public_model": model,
                    "upstream_model": "native-image-model",
                    "sizes": ["*"],
                    "qualities": ["*"],
                    "operations": ["generation"],
                    "cost_per_request": 0.08,
                }],
            }
        )
        captured = {}
        upstream_response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://proxy-image.example/v1/images/generations"),
            json={"data": [{"url": "https://cdn.example/image.png"}]},
        )

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, url, **kwargs):
                captured["url"] = url
                captured["payload"] = kwargs["json"]
                return upstream_response

        with patch("app.image_proxy.httpx.AsyncClient", return_value=MockAsyncClient()):
            result = asyncio.run(forward_json({"model": model, "prompt": "test"}, "generation"))

        self.assertEqual(result.status_code, 200)
        self.assertEqual(captured["payload"]["model"], "native-image-model")
        public_url = json.loads(result.body)["data"][0]["url"]
        self.assertTrue(public_url.startswith(f"{database.get_public_link_base_url()}/public/images/assets/img_"))
        self.assertNotIn("/v1/images/assets/", public_url)
        self.assertNotIn("cdn.example", public_url)

    def test_image_proxy_sanitizes_upstream_error_details(self):
        model = f"image-error-{time.time_ns()}"
        image_database.save_upstream(
            {
                "name": "error-image",
                "base_url": "https://error-image.example",
                "api_key": "error-key",
                "enabled": True,
                "priority": 1,
                "routes": [{
                    "public_model": model,
                    "upstream_model": "error-native",
                    "sizes": ["*"],
                    "qualities": ["*"],
                    "operations": ["generation"],
                    "cost_per_request": 0.05,
                }],
            }
        )
        upstream_response = httpx.Response(
            400,
            request=httpx.Request("POST", "https://error-image.example/v1/images/generations"),
            json={"error": {"message": "error-image.example private upstream message"}},
        )

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, *_args, **_kwargs):
                return upstream_response

        with patch("app.image_proxy.httpx.AsyncClient", return_value=MockAsyncClient()):
            result = asyncio.run(forward_json({"model": model, "prompt": "test"}, "generation"))

        self.assertEqual(result.status_code, 400)
        self.assertNotIn("error-image.example", result.body.decode())
        self.assertEqual(image_database.list_requests(model)[0]["health_outcome"], "neutral")

    def test_image_edit_forwards_multipart_fields_and_file(self):
        model = f"image-edit-{time.time_ns()}"
        image_database.save_upstream(
            {
                "name": "edit-image",
                "base_url": "https://edit-image.example/v1",
                "api_key": "edit-key",
                "enabled": True,
                "priority": 1,
                "routes": [{
                    "public_model": model,
                    "upstream_model": "native-edit-model",
                    "sizes": ["1k"],
                    "qualities": ["high"],
                    "operations": ["edit"],
                    "cost_per_request": 0.08,
                }],
            }
        )
        captured = {}
        upstream_response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://edit-image.example/v1/images/edits"),
            json={"created": 123, "data": [{"b64_json": "aW1hZ2U="}]},
        )

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, url, **kwargs):
                captured["url"] = url
                captured["headers"] = kwargs["headers"]
                for name, part in kwargs["files"]:
                    if name == "model":
                        captured["model"] = part[1]
                    elif name == "image":
                        captured["filename"] = part[0]
                        captured["image"] = part[1].read()
                        captured["content_type"] = part[2]
                return upstream_response

        client = TestClient(app)
        with patch("app.image_proxy.httpx.AsyncClient", return_value=MockAsyncClient()):
            response = client.post(
                "/v1/images/edits",
                headers={
                    "Authorization": "Bearer test-adapter-key",
                    "Idempotency-Key": "edit-idempotency-key",
                },
                data={"model": model, "prompt": "edit prompt", "size": "1024x1024", "quality": "high"},
                files={"image": ("source.png", b"image-bytes", "image/png")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["url"], "https://edit-image.example/v1/images/edits")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer edit-key")
        self.assertEqual(captured["headers"]["Idempotency-Key"], "edit-idempotency-key")
        self.assertEqual(captured["model"], "native-edit-model")
        self.assertEqual(captured["filename"], "source.png")
        self.assertEqual(captured["image"], b"image-bytes")
        self.assertEqual(captured["content_type"], "image/png")
        self.assertEqual(response.json()["data"][0]["b64_json"], "aW1hZ2U=")
        self.assertTrue(response.headers["X-Oneapi-Request-Id"].startswith("irq_"))
        self.assertEqual(image_database.list_requests(model)[0]["operation"], "edit")

    def test_image_asset_endpoint_uses_saved_source_url(self):
        source_url = "https://cdn.example/generated/image.png"
        asset_id = image_database.create_image_asset(source_url)
        captured = {}

        async def mock_stream(url, request, **kwargs):
            captured["url"] = url
            captured["timeout"] = kwargs["timeout"]
            captured["validator"] = kwargs["source_url_validator"]
            return Response(content=b"png-bytes", media_type="image/png")

        with patch("app.image_proxy.proxy.stream_upstream_content", new=mock_stream):
            client = TestClient(app)
            response = client.get(f"/v1/images/assets/{asset_id}")
            public_response = client.get(f"/public/images/assets/{asset_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"png-bytes")
        self.assertEqual(public_response.status_code, 200)
        self.assertEqual(public_response.content, b"png-bytes")
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(captured["url"], source_url)
        self.assertEqual(captured["timeout"], settings.image_upstream_timeout_seconds)
        self.assertTrue(captured["validator"](source_url))

    def test_image_admin_upstream_crud_preserves_existing_api_key(self):
        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        headers = {"X-CSRF-Token": csrf_token(session)}
        model = f"image-admin-{time.time_ns()}"
        payload = {
            "name": "admin-image",
            "base_url": "https://admin-image.example",
            "api_key": "original-image-key",
            "enabled": True,
            "priority": 7,
            "routes": [{
                "public_model": model,
                "upstream_model": "admin-native",
                "sizes": ["1k"],
                "qualities": ["medium"],
                "operations": ["generation"],
                "cost_per_request": 0.05,
            }],
        }

        created = client.post("/admin/api/images/upstreams", json=payload, headers=headers)
        self.assertEqual(created.status_code, 200)
        upstream_id = created.json()["id"]
        self.assertNotIn("api_key", created.json())

        payload["name"] = "admin-image-updated"
        payload["api_key"] = ""
        updated = client.put(
            f"/admin/api/images/upstreams/{upstream_id}", json=payload, headers=headers
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["name"], "admin-image-updated")
        self.assertEqual(
            image_database.get_upstream(upstream_id, include_key=True)["api_key"],
            "original-image-key",
        )

        deleted = client.delete(f"/admin/api/images/upstreams/{upstream_id}", headers=headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertIsNone(image_database.get_upstream(upstream_id))

    def test_image_model_discovery_uses_saved_key_and_normalizes_v1_url(self):
        upstream = image_database.save_upstream(
            {
                "name": "discover-image",
                "base_url": "https://discover-image.example/v1",
                "api_key": "saved-discovery-key",
                "enabled": True,
                "priority": 1,
                "routes": [{
                    "public_model": f"discover-placeholder-{time.time_ns()}",
                    "upstream_model": "discover-placeholder-native",
                    "sizes": ["*"],
                    "qualities": ["*"],
                    "operations": ["generation"],
                    "cost_per_request": 0.01,
                }],
            }
        )
        captured = {}
        upstream_response = httpx.Response(
            200,
            request=httpx.Request("GET", "https://discover-image.example/v1/models"),
            json={"data": [{"id": "image-alpha"}, {"id": "image-beta"}]},
        )

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def get(self, url, **kwargs):
                captured["url"] = url
                captured["headers"] = kwargs["headers"]
                return upstream_response

        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        with patch("app.main.httpx.AsyncClient", MockAsyncClient):
            response = client.post(
                "/admin/api/images/upstreams/models",
                headers={"X-CSRF-Token": csrf_token(session)},
                json={
                    "upstream_id": upstream["id"],
                    "base_url": "https://discover-image.example/v1",
                    "api_key": "",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["models"], ["image-alpha", "image-beta"])
        self.assertEqual(captured["url"], "https://discover-image.example/v1/models")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer saved-discovery-key")
        image_database.delete_upstream(upstream["id"])

    def test_admin_can_download_integration_document(self):
        client = TestClient(app)
        client.cookies.set(SESSION_COOKIE, create_session("admin"))
        original_base_url = database.get_public_link_base_url()
        original_limit = database.get_public_video_download_limit()
        try:
            database.set_public_link_base_url("https://media.yyapi.cloud")
            database.set_public_video_download_limit(7)
            response = client.get("/admin/api/integration-document")
        finally:
            database.set_public_link_base_url(original_base_url)
            database.set_public_video_download_limit(original_limit)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename=\"video-api-integration.md\"", response.headers["content-disposition"])
        self.assertIn("stable-manxue", response.text)
        self.assertNotIn("manxue-900-10s", response.text)
        self.assertIn("API Base URL：`https://zl.yyapi.cloud`", response.text)
        self.assertIn("GET https://zl.yyapi.cloud/v1/models", response.text)
        self.assertIn("POST https://zl.yyapi.cloud/v1/videos", response.text)
        self.assertNotIn(settings.public_base_url, response.text)
        self.assertNotRegex(response.text, r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
        self.assertNotIn("test-adapter-key", response.text)
        self.assertNotIn("private-key", response.text)
        self.assertNotIn("WORKBENCH_ORIGIN", response.text)
        self.assertNotIn("上游", response.text)
        self.assertNotIn("/v1/model-capabilities", response.text)
        self.assertNotIn("/new-api", response.text)
        self.assertIn("API Key 只发送到 API Base URL", response.text)
        self.assertIn("`data[].id` 是创建任务时应填写的 `model`", response.text)
        self.assertIn("`/v1/models` 响应中 `data[].id` 的值", response.text)
        self.assertIn('video_url = task.get("video_url")', response.text)
        self.assertIn('"status": "completed"', response.text)
        self.assertIn('"message": "Reference video duration must be between 2 and 15 seconds"', response.text)
        self.assertIn("最多 7 次", response.text)
        self.assertIn("https://media.yyapi.cloud/public/videos/task_xxx/content", response.text)
        self.assertNotIn("https://media.yyapi.cloud/v1/videos", response.text)
        self.assertIn("参考图片统一使用 `image_urls` 数组", response.text)
        self.assertIn("即使只有一张也使用数组", response.text)
        self.assertIn("/v1/videos", response.text)
        self.assertIn("/public/videos/task_xxx/content", response.text)
        self.assertNotIn("/v1/videos/task_xxx/content", response.text)

    def test_admin_can_download_image_integration_document(self):
        client = TestClient(app)
        self.assertEqual(client.get("/admin/api/image-integration-document").status_code, 401)
        client.cookies.set(SESSION_COOKIE, create_session("admin"))
        original_base_url = database.get_public_link_base_url()
        try:
            database.set_public_link_base_url("https://media.yyapi.cloud")
            response = client.get("/admin/api/image-integration-document")
        finally:
            database.set_public_link_base_url(original_base_url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename=\"image-api-integration.md\"", response.headers["content-disposition"])
        self.assertIn("Base URL：`https://zl.yyapi.cloud`", response.text)
        self.assertIn("https://media.yyapi.cloud/public/images/assets/{asset_id}", response.text)
        self.assertNotIn("https://media.yyapi.cloud/v1/images/generations", response.text)
        self.assertIn("/v1/images/generations", response.text)
        self.assertIn("/public/images/assets/{asset_id}", response.text)
        self.assertNotIn("/v1/images/assets/{asset_id}", response.text)
        self.assertIn("| `prompt` | string | 是 |", response.text)
        self.assertIn("`response_format`", response.text)
        self.assertIn("`output_compression`", response.text)
        self.assertIn("client.images.generate", response.text)
        self.assertIn("client.images.edit", response.text)
        self.assertIn("X-Oneapi-Request-Id", response.text)
        self.assertNotIn("适配器", response.text)
        self.assertNotIn("上游", response.text)
        self.assertNotIn("ADAPTER_API_KEY", response.text)

    def test_adapter_endpoint_requires_channel_key(self):
        response = TestClient(app).get("/v1/models")
        self.assertEqual(response.status_code, 401)

    def test_task_audit_endpoints_require_admin_session_and_csrf(self):
        client = TestClient(app)
        self.assertEqual(client.get("/admin/api/tasks").status_code, 401)
        self.assertEqual(client.get("/admin/api/tasks/vrq_missing").status_code, 401)
        self.assertEqual(client.get("/admin/api/tasks/vrq_missing/content").status_code, 401)

        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        self.assertEqual(client.get("/admin/api/tasks").status_code, 200)
        self.assertEqual(
            client.put(
                "/admin/api/tasks/vrq_missing/public-task",
                json={"public_task_id": "task_public_123"},
            ).status_code,
            403,
        )
        self.assertEqual(
            client.put(
                "/admin/api/tasks/vrq_missing/public-task",
                json={"public_task_id": "task_bad/path"},
                headers={"X-CSRF-Token": csrf_token(session)},
            ).status_code,
            422,
        )

    def test_public_link_settings_are_validated_and_persisted(self):
        client = TestClient(app)
        self.assertEqual(
            client.put(
                "/admin/api/settings/public-link",
                json={"public_base_url": "https://untrusted.example"},
            ).status_code,
            401,
        )

        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        headers = {"X-CSRF-Token": csrf_token(session)}
        self.assertEqual(
            client.put(
                "/admin/api/settings/public-link",
                json={"public_base_url": "https://untrusted.example"},
                headers=headers,
            ).status_code,
            422,
        )

        original = database.get_public_link_base_url()
        try:
            response = client.put(
                "/admin/api/settings/public-link",
                json={"public_base_url": "https://media.yyapi.cloud"},
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["public_link_base_url"], "https://media.yyapi.cloud")
            dashboard = client.get("/admin/api/dashboard").json()
            self.assertEqual(dashboard["public_link_base_url"], "https://media.yyapi.cloud")
            self.assertEqual(
                dashboard["public_link_base_url_options"],
                [
                    "https://media.yyapi.cloud",
                    "https://www.yyapi.cloud",
                    "https://zl.yyapi.cloud",
                ],
            )
        finally:
            database.set_public_link_base_url(original)

    def test_public_link_setting_changes_sanitized_urls(self):
        relay_request_id = database.start_audit_request(
            self.upstream["id"],
            "audit-model",
            "videos",
            {"model": "audit-model", "prompt": "domain selection"},
        )
        database.create_task(
            "upstream-domain-task",
            self.upstream["id"],
            relay_request_id,
            "audit-model",
            "videos",
            "queued",
        )
        database.record_audit_event(
            relay_request_id,
            "poll",
            200,
            '{"video_url":"https://private-upstream.example/domain.mp4"}',
            {"status": "completed"},
        )
        database.update_task(
            "upstream-domain-task",
            "completed",
            "https://private-upstream.example/domain.mp4",
            None,
        )
        database.set_public_task_id(relay_request_id, "task_domain_selection")

        original = database.get_public_link_base_url()
        try:
            database.set_public_link_base_url("https://www.yyapi.cloud")
            detail = database.get_audit_request(relay_request_id)
            expected = "https://www.yyapi.cloud/public/videos/task_domain_selection/content"
            self.assertEqual(detail["sanitized_video_url"], expected)
            for field in ("url", "video_url", "result_url", "download_url"):
                self.assertEqual(detail["events"][0]["sanitized_body"][field], expected)
        finally:
            database.set_public_link_base_url(original)

    def test_completed_task_query_returns_selected_public_video_url(self):
        task_id = "query-domain-task"
        relay_request_id = database.start_audit_request(
            self.upstream["id"],
            "audit-model",
            "videos",
            {"model": "audit-model", "prompt": "query domain"},
        )
        database.create_task(task_id, self.upstream["id"], relay_request_id, "audit-model", "videos", "queued")
        database.set_public_task_id(relay_request_id, "task_query_domain")
        response = httpx.Response(
            200,
            request=httpx.Request("GET", "https://private-upstream.example/v1/videos/query-domain-task"),
            json={"status": "completed", "progress": 100},
        )

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def get(self, *_args, **_kwargs):
                return response

        original = database.get_public_link_base_url()
        try:
            database.set_public_link_base_url("https://www.yyapi.cloud")
            with patch("app.proxy.httpx.AsyncClient", return_value=MockAsyncClient()):
                result = asyncio.run(fetch_task(task_id))
            payload = json.loads(result.body)
            self.assertEqual(
                payload["video_url"],
                "https://www.yyapi.cloud/public/videos/task_query_domain/content",
            )
        finally:
            database.set_public_link_base_url(original)

    def test_public_video_download_limit_setting_is_admin_only_and_persisted(self):
        client = TestClient(app)
        self.assertEqual(
            client.put("/admin/api/settings/public-video", json={"download_limit": 2}).status_code,
            401,
        )

        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        headers = {"X-CSRF-Token": csrf_token(session)}
        for invalid_limit in (0, 10001):
            self.assertEqual(
                client.put(
                    "/admin/api/settings/public-video",
                    json={"download_limit": invalid_limit},
                    headers=headers,
                ).status_code,
                422,
            )

        original = database.get_public_video_download_limit()
        try:
            response = client.put(
                "/admin/api/settings/public-video",
                json={"download_limit": 2},
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["public_video_download_limit"], 2)
            self.assertEqual(client.get("/admin/api/dashboard").json()["public_video_download_limit"], 2)
        finally:
            database.set_public_video_download_limit(original)

    def test_public_video_route_uses_configured_limit_and_allows_range_requests(self):
        relay_request_id = database.start_audit_request(
            self.upstream["id"],
            "audit-model",
            "videos",
            {"model": "audit-model", "prompt": "public download"},
        )
        task_id = f"public-download-{time.time_ns()}"
        public_task_id = f"task_public_download_{time.time_ns()}"
        database.create_task(
            task_id,
            self.upstream["id"],
            relay_request_id,
            "audit-model",
            "videos",
            "queued",
        )
        database.update_task(task_id, "completed", "https://cdn.example/public.mp4", None)
        self.assertTrue(database.set_public_task_id(relay_request_id, public_task_id))

        async def mock_stream(selected_task_id, request):
            self.assertEqual(selected_task_id, task_id)
            return Response(content=b"video", media_type="video/mp4")

        original_limit = database.get_public_video_download_limit()
        try:
            database.set_public_video_download_limit(2)
            client = TestClient(app)
            with patch("app.main.proxy.stream_content", new=mock_stream):
                first = client.get(f"/public/videos/{public_task_id}/content")
                range_request = client.get(
                    f"/public/videos/{public_task_id}/content",
                    headers={"Range": "bytes=100-200"},
                )
                second = client.get(f"/public/videos/{public_task_id}/content")
                limited = client.get(f"/public/videos/{public_task_id}/content")

            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.headers["cache-control"], "private, no-store")
            self.assertEqual(range_request.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(limited.status_code, 429)
            with database.connection() as conn:
                count = conn.execute(
                    "SELECT public_download_count FROM audit_requests WHERE relay_request_id = ?",
                    (relay_request_id,),
                ).fetchone()["public_download_count"]
            self.assertEqual(count, 2)
        finally:
            database.set_public_video_download_limit(original_limit)

    def test_public_video_route_releases_count_when_stream_setup_fails(self):
        relay_request_id = database.start_audit_request(
            self.upstream["id"],
            "audit-model",
            "videos",
            {"model": "audit-model", "prompt": "public download failure"},
        )
        task_id = f"public-download-failure-{time.time_ns()}"
        public_task_id = f"task_public_download_failure_{time.time_ns()}"
        database.create_task(
            task_id,
            self.upstream["id"],
            relay_request_id,
            "audit-model",
            "videos",
            "queued",
        )
        database.update_task(task_id, "completed", "https://cdn.example/public.mp4", None)
        self.assertTrue(database.set_public_task_id(relay_request_id, public_task_id))

        async def failed_stream(*_args, **_kwargs):
            raise RuntimeError("upstream unavailable")

        client = TestClient(app)
        with patch("app.main.proxy.stream_content", new=failed_stream):
            with self.assertRaises(RuntimeError):
                client.get(f"/public/videos/{public_task_id}/content")

        with database.connection() as conn:
            count = conn.execute(
                "SELECT public_download_count FROM audit_requests WHERE relay_request_id = ?",
                (relay_request_id,),
            ).fetchone()["public_download_count"]
        self.assertEqual(count, 0)

    def test_video_stream_does_not_send_api_key_to_cross_origin_signed_url(self):
        relay_request_id = database.start_audit_request(
            self.upstream["id"],
            "audit-model",
            "videos",
            {"model": "audit-model", "prompt": "signed video"},
        )
        task_id = f"signed-video-{time.time_ns()}"
        database.create_task(
            task_id,
            self.upstream["id"],
            relay_request_id,
            "audit-model",
            "videos",
            "completed",
        )
        signed_url = "https://asset.ai666.live/video.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        database.update_task(task_id, "completed", signed_url, None)
        captured = {}

        async def mock_stream(source_url, _request, headers=None, **_kwargs):
            captured["source_url"] = source_url
            captured["headers"] = headers
            return Response(content=b"video", media_type="video/mp4")

        request = httpx.Request("GET", "https://media.yyapi.cloud/public/videos/task_public/content")
        with patch("app.proxy.stream_upstream_content", new=mock_stream):
            asyncio.run(stream_content(task_id, request))

        self.assertEqual(captured["source_url"], signed_url)
        self.assertNotIn("Authorization", captured["headers"])

    def test_video_stream_keeps_api_key_for_same_origin_content(self):
        relay_request_id = database.start_audit_request(
            self.upstream["id"],
            "audit-model",
            "videos",
            {"model": "audit-model", "prompt": "same-origin video"},
        )
        task_id = f"same-origin-video-{time.time_ns()}"
        database.create_task(
            task_id,
            self.upstream["id"],
            relay_request_id,
            "audit-model",
            "videos",
            "completed",
        )
        content_url = "https://private-upstream.example/v1/videos/source/content"
        database.update_task(task_id, "completed", content_url, None)
        captured = {}

        async def mock_stream(_source_url, _request, headers=None, **_kwargs):
            captured["headers"] = headers
            return Response(content=b"video", media_type="video/mp4")

        request = httpx.Request("GET", "https://media.yyapi.cloud/public/videos/task_public/content")
        with patch("app.proxy.stream_upstream_content", new=mock_stream):
            asyncio.run(stream_content(task_id, request))

        self.assertEqual(captured["headers"]["Authorization"], "Bearer private-key")

    def test_initialize_migrates_legacy_tasks_without_data_loss(self):
        with tempfile.TemporaryDirectory() as data_dir:
            legacy_path = Path(data_dir) / "adapter.db"
            conn = sqlite3.connect(legacy_path)
            conn.executescript(
                """
                CREATE TABLE upstreams (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 100,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_used_at INTEGER
                );
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    upstream_id INTEGER NOT NULL REFERENCES upstreams(id),
                    model TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    source_video_url TEXT,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT INTO upstreams(
                    id, name, base_url, api_key_encrypted, enabled, priority, created_at, updated_at
                ) VALUES (1, 'legacy', 'https://legacy.example', ?, 1, 1, 100, 100)
                """,
                (secret_box.encrypt("legacy-key"),),
            )
            conn.execute(
                """
                INSERT INTO tasks(
                    task_id, upstream_id, model, protocol, status, source_video_url, created_at, updated_at
                ) VALUES (
                    'legacy-upstream-task', 1, 'legacy-model', 'videos', 'completed',
                    'https://legacy.example/video.mp4', 100, 200
                )
                """
            )
            conn.commit()
            conn.close()

            with patch.object(database, "DB_PATH", legacy_path):
                database.initialize()
                with database.connection() as migrated:
                    task = migrated.execute(
                        "SELECT relay_request_id FROM tasks WHERE task_id = 'legacy-upstream-task'"
                    ).fetchone()
                self.assertTrue(task["relay_request_id"].startswith("vrq_legacy_"))
                detail = database.get_audit_request(task["relay_request_id"])

            self.assertIsNotNone(detail)
            self.assertEqual(detail["upstream_task_id"], "legacy-upstream-task")
            self.assertEqual(detail["source_video_url"], "https://legacy.example/video.mp4")

    def test_initialize_migrates_legacy_model_profiles(self):
        with tempfile.TemporaryDirectory() as data_dir:
            legacy_path = Path(data_dir) / "adapter.db"
            conn = sqlite3.connect(legacy_path)
            conn.executescript(
                """
                CREATE TABLE upstreams (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 100,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_used_at INTEGER
                );
                CREATE TABLE model_routes (
                    id INTEGER PRIMARY KEY,
                    upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    protocol TEXT NOT NULL,
                    UNIQUE(upstream_id, model)
                );
                """
            )
            conn.execute(
                """
                INSERT INTO upstreams(id, name, base_url, api_key_encrypted, enabled, priority, created_at, updated_at)
                VALUES (1, 'legacy', 'https://legacy.example', ?, 1, 1, 100, 100)
                """,
                (secret_box.encrypt("legacy-key"),),
            )
            conn.execute(
                "INSERT INTO model_routes(upstream_id, model, protocol) VALUES (1, 'manxue-933', 'videos')"
            )
            conn.commit()
            conn.close()

            with patch.object(database, "DB_PATH", legacy_path):
                database.initialize()
                with database.connection() as migrated:
                    route = migrated.execute(
                        "SELECT profile, duration_override, upstream_model FROM model_routes WHERE model = 'manxue-933'"
                    ).fetchone()

            self.assertEqual(route["profile"], "manxue-933")
            self.assertIsNone(route["duration_override"])
            self.assertEqual(route["upstream_model"], "manxue-933")

    def test_initialize_expands_legacy_protocol_constraint_for_ark_v3(self):
        with tempfile.TemporaryDirectory() as data_dir:
            database_path = Path(data_dir) / "adapter.db"
            conn = sqlite3.connect(database_path)
            conn.executescript(
                """
                CREATE TABLE upstreams (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    priority INTEGER NOT NULL DEFAULT 100,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_used_at INTEGER
                );
                CREATE TABLE model_routes (
                    id INTEGER PRIMARY KEY,
                    upstream_id INTEGER NOT NULL REFERENCES upstreams(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    upstream_model TEXT NOT NULL,
                    protocol TEXT NOT NULL CHECK(protocol IN ('videos', 'seedance')),
                    profile TEXT NOT NULL DEFAULT 'default',
                    duration_override INTEGER,
                    UNIQUE(upstream_id, model)
                );
                """
            )
            conn.execute(
                """
                INSERT INTO upstreams(id, name, base_url, api_key_encrypted, enabled, priority, created_at, updated_at)
                VALUES (1, 'legacy-protocol', 'https://legacy.example', ?, 1, 1, 100, 100)
                """,
                (secret_box.encrypt("legacy-key"),),
            )
            conn.execute(
                """
                INSERT INTO model_routes(upstream_id, model, upstream_model, protocol, profile)
                VALUES (1, 'legacy-video', 'legacy-video', 'videos', 'default')
                """
            )
            conn.commit()
            conn.close()

            with patch.object(database, "DB_PATH", database_path):
                database.initialize()
                database.save_upstream({
                    "name": "ark",
                    "base_url": "https://ark.example",
                    "api_key": "ark-key",
                    "enabled": True,
                    "priority": 1,
                    "routes": [{
                        "model": "ark-public",
                        "upstream_model": "doubao-seedance-2-0-260128",
                        "protocol": "ark-v3",
                        "profile": "ark-seedance-2",
                        "durations": [4, 15],
                    }],
                })
                with database.connection() as migrated:
                    table_sql = migrated.execute(
                        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'model_routes'"
                    ).fetchone()["sql"]
                    routes = migrated.execute(
                        "SELECT model, protocol FROM model_routes ORDER BY model"
                    ).fetchall()

            self.assertIn("ark-v3", table_sql)
            self.assertEqual([(route["model"], route["protocol"]) for route in routes], [
                ("ark-public", "ark-v3"),
                ("legacy-video", "videos"),
            ])

    def test_initialize_preserves_explicit_profile_for_mapped_omni_route(self):
        with tempfile.TemporaryDirectory() as data_dir:
            database_path = Path(data_dir) / "adapter.db"
            with patch.object(database, "DB_PATH", database_path):
                database.initialize()
                with database.connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO upstreams(
                            id, name, base_url, api_key_encrypted, enabled, priority, created_at, updated_at
                        ) VALUES (1, 'omni', 'https://omni.example', ?, 1, 1, 100, 100)
                        """,
                        (secret_box.encrypt("omni-key"),),
                    )
                    conn.execute(
                        """
                        INSERT INTO model_routes(
                            upstream_id, model, upstream_model, protocol, profile, durations_json
                        ) VALUES (1, 'gemini-omni-flash', 'omni-flash-720p', 'videos', 'manxue-933', '[]')
                        """
                    )
                database.initialize()
                with database.connection() as conn:
                    route = conn.execute(
                        "SELECT profile FROM model_routes WHERE model = 'gemini-omni-flash'"
                    ).fetchone()

            self.assertEqual(route["profile"], "manxue-933")

    def test_initialize_preserves_saved_profiles_for_933_upstream_aliases(self):
        with tempfile.TemporaryDirectory() as data_dir:
            database_path = Path(data_dir) / "adapter.db"
            with patch.object(database, "DB_PATH", database_path):
                database.initialize()
                with database.connection() as conn:
                    conn.execute(
                        """
                        INSERT INTO upstreams(
                            id, name, base_url, api_key_encrypted, enabled, priority, created_at, updated_at
                        ) VALUES (1, '933', 'https://933.example', ?, 1, 1, 100, 100)
                        """,
                        (secret_box.encrypt("933-key"),),
                    )
                    conn.executemany(
                        """
                        INSERT INTO model_routes(
                            upstream_id, model, upstream_model, protocol, profile, durations_json
                        ) VALUES (1, ?, ?, 'videos', ?, '[]')
                        """,
                        [
                            ("manxue-933", "sora-v3-933-pro", "default"),
                            ("manxue-900-10s", "tejiasd2", "gemini-omni"),
                        ],
                    )
                database.initialize()
                with database.connection() as conn:
                    routes = conn.execute(
                        "SELECT upstream_model, profile FROM model_routes ORDER BY upstream_model"
                    ).fetchall()

            self.assertEqual({route["upstream_model"]: route["profile"] for route in routes}, {
                "sora-v3-933-pro": "default",
                "tejiasd2": "gemini-omni",
            })

    def test_status_mapping(self):
        expected = {
            "NOT_START": "queued",
            "IN_PROGRESS": "processing",
            "SUCCESS": "completed",
            "FAILURE": "failed",
            "expired": "failed",
            "processing": "processing",
        }
        for source, target in expected.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_status(source), target)

    def test_seedance_response_normalization(self):
        task = {
            "task_id": "task_123",
            "model": "seedance-2.0-fast",
            "protocol": "seedance",
            "created_at": 100,
        }
        payload = {"data": {"status": "SUCCESS", "data": {"content": {"video_url": "https://cdn/video.mp4"}}}}
        result, video_url = normalize_task_payload(task, payload)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["progress"], 100)
        self.assertEqual(video_url, "https://cdn/video.mp4")
        self.assertNotIn("id", result)
        self.assertNotIn("task_id", result)
        self.assertNotIn("video_url", result)

    def test_ark_v3_create_poll_and_cross_origin_download(self):
        model = f"ark-public-{time.time_ns()}"
        upstream = database.save_upstream({
            "name": f"ark-{time.time_ns()}",
            "base_url": "https://ark.example",
            "api_key": "ark-key",
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": model,
                "upstream_model": "doubao-seedance-2-0-260128",
                "protocol": "ark-v3",
                "profile": "ark-seedance-2",
                "durations": [4, 15],
                "image_count": 9,
                "supports_video": True,
                "supports_audio": True,
            }],
        })
        task_id = f"cgt-{time.time_ns()}"
        captured = {}

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, url, **kwargs):
                captured["create_url"] = url
                captured["create_headers"] = kwargs["headers"]
                captured["create_payload"] = kwargs["json"]
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={"id": task_id},
                )

            async def get(self, url, **kwargs):
                captured["poll_url"] = url
                captured["poll_headers"] = kwargs["headers"]
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", url),
                    json={
                        "id": task_id,
                        "status": "succeeded",
                        "content": {"video_url": "https://ark-cdn.example/result.mp4?token=temporary"},
                    },
                )

        with patch("app.proxy.httpx.AsyncClient", return_value=MockAsyncClient()):
            created = asyncio.run(create_video({
                "model": model,
                "prompt": "电影感运镜",
                "aspect_ratio": "16:9",
                "duration": 15,
                "resolution": "720p",
                "generate_audio": True,
                "image_urls": ["https://cdn.example/reference.png"],
                "reference_video": "https://cdn.example/reference.mp4",
                "audio_urls": ["https://cdn.example/reference.mp3"],
            }, None))
            fetched = asyncio.run(fetch_task(task_id))

        self.assertEqual(created.status_code, 200)
        self.assertEqual(json.loads(fetched.body)["status"], "completed")
        self.assertEqual(captured["create_url"], "https://ark.example/api/v3/contents/generations/tasks")
        self.assertEqual(
            captured["poll_url"],
            f"https://ark.example/api/v3/contents/generations/tasks/{task_id}",
        )
        self.assertNotIn("Idempotency-Key", captured["create_headers"])
        self.assertEqual(captured["create_payload"]["model"], "doubao-seedance-2-0-260128")
        self.assertEqual(captured["create_payload"]["ratio"], "16:9")
        self.assertEqual([item.get("role") for item in captured["create_payload"]["content"][1:]], [
            "reference_image",
            "reference_video",
            "reference_audio",
        ])
        self.assertEqual(
            database.get_task(task_id)["source_video_url"],
            "https://ark-cdn.example/result.mp4?token=temporary",
        )

        async def mock_stream(source_url, _request, headers=None, **_kwargs):
            captured["download_url"] = source_url
            captured["download_headers"] = headers
            return Response(content=b"video", media_type="video/mp4")

        request = httpx.Request("GET", f"https://media.yyapi.cloud/public/videos/{task_id}/content")
        with patch("app.proxy.stream_upstream_content", new=mock_stream):
            asyncio.run(stream_content(task_id, request))

        self.assertEqual(captured["download_url"], "https://ark-cdn.example/result.mp4?token=temporary")
        self.assertNotIn("Authorization", captured["download_headers"])
        self.assertEqual(upstream["routes"][0]["protocol"], "ark-v3")

    def test_task_response_keeps_upstream_details_internal(self):
        task = {
            "task_id": "upstream-secret-id",
            "model": "sora-v3-933-pro",
            "protocol": "videos",
            "created_at": 100,
        }
        payload = {
            "id": "upstream-secret-id",
            "task_id": "upstream-secret-id",
            "status": "completed",
            "progress": "100",
            "video_url": "https://api.pixellelabs.com/v4/generated/private.mp4",
        }

        result, video_url = normalize_task_payload(task, payload)
        body = json.dumps(result)

        self.assertEqual(video_url, payload["video_url"])
        self.assertEqual(result["status"], "completed")
        self.assertNotIn("pixellelabs", body)
        self.assertNotIn("upstream-secret-id", body)
        self.assertNotIn("video_url", result)
        self.assertNotIn("task_id", result)

    def test_session_and_secret_round_trip(self):
        before = int(time.time())
        session = create_session("admin")
        payload = read_session(session)
        self.assertEqual(payload["username"], "admin")
        self.assertGreaterEqual(payload["expires_at"], before + settings.session_ttl_seconds)
        self.assertLessEqual(payload["expires_at"], int(time.time()) + settings.session_ttl_seconds)
        encrypted = secret_box.encrypt("sk-secret")
        self.assertNotIn("sk-secret", encrypted)
        self.assertEqual(secret_box.decrypt(encrypted), "sk-secret")

    def test_login_cookie_uses_configured_session_lifetime(self):
        response = TestClient(app).post(
            "/admin/api/login",
            json={"username": "admin", "password": "test-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"Max-Age={settings.session_ttl_seconds}", response.headers["set-cookie"])

    def test_model_discovery_normalizes_upstream_formats(self):
        payload = {
            "data": [
                {"id": "sora-v3-933-pro"},
                {"model": "seedance-2.0-fast"},
                "veo31-fast",
                {"id": "sora-v3-933-pro"},
                {"id": ""},
            ]
        }
        self.assertEqual(
            normalize_discovered_models(payload),
            [
                {"model": "", "upstream_model": "sora-v3-933-pro", "protocol": "videos", "profile": "manxue-933", "durations": []},
                {"model": "", "upstream_model": "seedance-2.0-fast", "protocol": "seedance", "profile": "default", "durations": []},
                {"model": "", "upstream_model": "veo31-fast", "protocol": "videos", "profile": "veo31-fast", "durations": []},
            ],
        )

    def test_model_discovery_selects_gemini_omni_profile_for_native_alias(self):
        models = normalize_discovered_models({"data": [{"id": "omni-flash-720p"}]})

        self.assertEqual(models[0]["profile"], "gemini-omni")

    def test_model_discovery_falls_back_to_ark_v3_endpoint(self):
        captured = []

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def get(self, url, **_kwargs):
                captured.append(url)
                if url.endswith("/v1/models"):
                    return httpx.Response(404, request=httpx.Request("GET", url), json={"error": "not found"})
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", url),
                    json={"data": [{"id": "doubao-seedance-2-0-260128"}]},
                )

        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        with patch("app.main.httpx.AsyncClient", MockAsyncClient):
            response = client.post(
                "/admin/api/upstreams/models",
                headers={"X-CSRF-Token": csrf_token(session)},
                json={
                    "base_url": "https://ark.example",
                    "api_key": "ark-key",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured, [
            "https://ark.example/v1/models",
            "https://ark.example/api/v3/models",
        ])
        self.assertEqual(response.json()["models"], [{
            "model": "",
            "upstream_model": "doubao-seedance-2-0-260128",
            "protocol": "ark-v3",
            "profile": "ark-seedance-2",
            "durations": [],
        }])

    def test_model_discovery_selects_933_profile_for_native_aliases(self):
        models = normalize_discovered_models({"data": ["sora-v3-933-pro", "tejiasd2"]})

        self.assertEqual([model["profile"] for model in models], ["manxue-933", "manxue-933"])

    def test_public_model_alias_is_used_without_exposing_upstream_name(self):
        client = TestClient(app)
        models = client.get("/v1/models", headers={"Authorization": "Bearer test-adapter-key"})
        model_ids = [item["id"] for item in models.json()["data"]]
        self.assertIn("stable-manxue", model_ids)
        self.assertNotIn("manxue-900-10s", model_ids)

        capabilities = client.get("/v1/model-capabilities").json()["data"]
        stable = next(item for item in capabilities if item["id"] == "stable-manxue")
        self.assertEqual(stable["capabilities"]["durations"], [10])

        captured = {}
        response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://private-upstream.example/v1/videos"),
            json={"task_id": "alias-upstream-task", "status": "queued"},
        )

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, *_args, **kwargs):
                captured["payload"] = kwargs["json"]
                return response

        with patch("app.proxy.httpx.AsyncClient", return_value=MockAsyncClient()):
            result = asyncio.run(create_video({"model": "stable-manxue", "prompt": "test", "duration": 10}, None))

        self.assertEqual(result.status_code, 200)
        self.assertEqual(captured["payload"]["model"], "manxue-900-10s")
        self.assertEqual(captured["payload"]["seconds"], "10")

    def test_route_supports_multiple_custom_durations(self):
        upstream = database.save_upstream(
            {
                "name": "duration-options",
                "base_url": "https://duration-options.example",
                "api_key": "duration-key",
                "enabled": True,
                "priority": 50,
                "routes": [
                    {
                        "model": "duration-options-model",
                        "upstream_model": "duration-options-upstream",
                        "protocol": "videos",
                        "profile": "default",
                        "durations": [4, 6, 30],
                    }
                ],
            }
        )
        try:
            route = database.get_upstream(upstream["id"])["routes"][0]
            self.assertEqual(route["durations"], [4, 6, 30])
            capabilities = next(
                item for item in database.list_model_capabilities() if item["id"] == "duration-options-model"
            )
            self.assertEqual(capabilities["capabilities"]["durations"], [4, 6, 30])
        finally:
            database.delete_upstream(upstream["id"])

    def test_upstream_model_rename_preserves_selected_profile(self):
        upstream = database.save_upstream(
            {
                "name": "rename-profile",
                "base_url": "https://rename-profile.example",
                "api_key": "rename-key",
                "enabled": True,
                "priority": 50,
                "routes": [{
                    "model": "stable-public-name",
                    "upstream_model": "old-native-name",
                    "protocol": "videos",
                    "profile": "manxue-933",
                    "durations": [10, 15],
                    "image_count": 7,
                    "supports_video": False,
                    "supports_audio": True,
                }],
            }
        )
        try:
            updated = database.save_upstream(
                {
                    "name": "rename-profile",
                    "base_url": "https://rename-profile.example",
                    "api_key": "",
                    "enabled": True,
                    "priority": 50,
                    "routes": [{
                        "model": "stable-public-name",
                        "upstream_model": "new-native-name",
                        "protocol": "videos",
                        "profile": "manxue-933",
                        "durations": [10, 15],
                        "image_count": 7,
                        "supports_video": False,
                        "supports_audio": True,
                    }],
                },
                upstream["id"],
            )

            self.assertEqual(updated["routes"][0]["upstream_model"], "new-native-name")
            self.assertEqual(updated["routes"][0]["profile"], "manxue-933")
            self.assertEqual(updated["routes"][0]["durations"], [10, 15])
            self.assertEqual(updated["routes"][0]["image_count"], 7)
            self.assertFalse(updated["routes"][0]["supports_video"])
            self.assertTrue(updated["routes"][0]["supports_audio"])

            capabilities = next(
                item for item in database.list_model_capabilities() if item["id"] == "stable-public-name"
            )["capabilities"]
            self.assertEqual(capabilities["durations"], [10, 15])
            self.assertEqual(capabilities["maxImages"], 7)
            self.assertFalse(capabilities["referenceVideo"])
            self.assertEqual(capabilities["maxAudios"], 3)
        finally:
            database.delete_upstream(upstream["id"])

    def test_manxue_profile_transforms_canonical_payload(self):
        payload = transform_create_payload(
            {
                "model": "manxue-900-10s",
                "prompt": "test",
                "aspect_ratio": "16:9",
                "duration": 10,
                "resolution": "720p",
                "generate_audio": True,
                "image_urls": ["https://cdn/main.png", "https://cdn/ref.png"],
                "reference_video": "https://cdn/ref.mp4",
                "audio_urls": ["https://cdn/voice.mp3"],
            },
            "manxue-933",
        )
        self.assertEqual(
            payload,
            {
                "model": "manxue-900-10s",
                "prompt": "test",
                "aspect_ratio": "16:9",
                "seconds": "10",
                "resolution": "720p",
                "generate_audio": True,
                "image_url": "https://cdn/main.png",
                "reference_image_urls": ["https://cdn/ref.png"],
                "reference_videos": ["https://cdn/ref.mp4"],
                "audio_urls": ["https://cdn/voice.mp3"],
            },
        )
        self.assertEqual(capabilities_for("manxue-933", 10)["durations"], [10])

    def test_gemini_omni_profile_uses_images_array_for_single_reference(self):
        payload = transform_create_payload(
            {
                "model": "omni-flash-720p",
                "prompt": "test @图1",
                "aspect_ratio": "16:9",
                "duration": 10,
                "resolution": "720p",
                "generate_audio": True,
                "image_urls": ["https://cdn/reference.png"],
            },
            "gemini-omni",
        )

        self.assertEqual(payload["images"], ["https://cdn/reference.png"])
        self.assertNotIn("image_url", payload)
        self.assertNotIn("image_urls", payload)
        self.assertEqual(capabilities_for("gemini-omni")["resolutions"], ["720p"])

    def test_gemini_omni_profile_accepts_upstream_images_input(self):
        images = ["https://cdn/one.png", "https://cdn/two.png"]

        payload = transform_create_payload(
            {"model": "omni-flash-720p", "prompt": "test", "images": images},
            "gemini-omni",
        )

        self.assertEqual(payload["images"], images)

    def test_gemini_omni_route_forwards_images_array_to_upstream(self):
        upstream = database.save_upstream(
            {
                "name": "omni-upstream",
                "base_url": "https://omni.example",
                "api_key": "omni-key",
                "enabled": True,
                "priority": 1,
                "routes": [{
                    "model": "gemini-omni-flash",
                    "upstream_model": "omni-flash-720p",
                    "protocol": "videos",
                    "profile": "gemini-omni",
                }],
            }
        )
        captured = {}
        response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://omni.example/v1/videos"),
            json={"task_id": "omni-task", "status": "queued"},
        )

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, *_args, **kwargs):
                captured["payload"] = kwargs["json"]
                return response

        try:
            with patch("app.proxy.httpx.AsyncClient", return_value=MockAsyncClient()):
                result = asyncio.run(create_video({
                    "model": "gemini-omni-flash",
                    "prompt": "test @图1",
                    "image_urls": ["https://cdn/reference.png"],
                }, None))
            detail = database.get_audit_request(result.headers["X-Oneapi-Request-Id"])
            with database.connection() as conn:
                encrypted_payload = conn.execute(
                    "SELECT upstream_request_payload_encrypted FROM audit_requests WHERE relay_request_id = ?",
                    (result.headers["X-Oneapi-Request-Id"],),
                ).fetchone()["upstream_request_payload_encrypted"]
        finally:
            with database.connection() as conn:
                conn.execute("DELETE FROM tasks WHERE upstream_id = ?", (upstream["id"],))
                conn.execute("DELETE FROM audit_requests WHERE upstream_id = ?", (upstream["id"],))
            database.delete_upstream(upstream["id"])

        self.assertEqual(result.status_code, 200)
        self.assertEqual(captured["payload"]["model"], "omni-flash-720p")
        self.assertEqual(captured["payload"]["images"], ["https://cdn/reference.png"])
        self.assertNotIn("image_url", captured["payload"])
        self.assertEqual(detail["upstream_request_payload"], captured["payload"])
        self.assertNotIn("image_urls", detail["upstream_request_payload"])
        self.assertNotIn("https://cdn/reference.png", encrypted_payload)

    def test_model_capabilities_are_public_and_cors_limited(self):
        response = TestClient(app).get(
            "/v1/model-capabilities",
            headers={"Origin": "https://image.yyapi.cloud"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://image.yyapi.cloud")
        self.assertTrue(any(item["id"] == "audit-model" for item in response.json()["data"]))

    def test_new_api_video_gateway_preserves_user_auth_body_and_response(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                request=request,
                json={"task_id": "task_public_123", "status": "queued"},
                headers={"X-Oneapi-Request-Id": "req-123"},
            )

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient

        def gateway_client(*args, **kwargs):
            return real_async_client(transport=transport, follow_redirects=False)

        client = TestClient(app)
        with patch("app.new_api_gateway.httpx.AsyncClient", side_effect=gateway_client):
            response = client.post(
                "/new-api/v1/videos",
                headers={
                    "Authorization": "Bearer user-new-api-key",
                    "Origin": "https://image.yyapi.cloud",
                },
                json={"model": "gemini-omni-flash", "prompt": "test"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["task_id"], "task_public_123")
        self.assertEqual(response.headers["x-oneapi-request-id"], "req-123")
        self.assertEqual(response.headers["access-control-allow-origin"], "https://image.yyapi.cloud")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], f"{settings.new_api_gateway_base_url}/v1/videos")
        self.assertEqual(captured["authorization"], "Bearer user-new-api-key")
        self.assertEqual(captured["body"]["model"], "gemini-omni-flash")

    def test_new_api_gateway_forwards_upload_presign_request(self):
        captured = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["method"] = request.method
            captured["url"] = str(request.url)
            captured["authorization"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                request=request,
                json={
                    "url": "https://tos.example/upload?signature=temporary",
                    "public_url": "https://tos.example/uploads/reference.png",
                    "method": "PUT",
                    "headers": {"Content-Type": "image/png"},
                    "expires_at": 1784682900,
                },
            )

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient

        def gateway_client(*_args, **_kwargs):
            return real_async_client(transport=transport, follow_redirects=False)

        client = TestClient(app)
        with patch("app.new_api_gateway.httpx.AsyncClient", side_effect=gateway_client):
            response = client.post(
                "/new-api/v1/upload/presign",
                headers={"Authorization": "Bearer user-new-api-key"},
                json={"filename": "reference.png", "content_type": "image/png"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["method"], "PUT")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], f"{settings.new_api_gateway_base_url}/v1/upload/presign")
        self.assertEqual(captured["authorization"], "Bearer user-new-api-key")
        self.assertEqual(captured["body"], {"filename": "reference.png", "content_type": "image/png"})

    def test_new_api_video_gateway_preflight_allows_workbench_post(self):
        response = TestClient(app).options(
            "/new-api/v1/videos",
            headers={
                "Origin": "https://image.yyapi.cloud",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://image.yyapi.cloud")
        self.assertIn("POST", response.headers["access-control-allow-methods"])

    def test_media_cors_exposes_stream_metadata(self):
        response = TestClient(app).get(
            "/healthz",
            headers={"Origin": "https://image.yyapi.cloud"},
        )
        self.assertEqual(response.status_code, 200)
        exposed = response.headers["access-control-expose-headers"].lower()
        for header in ("content-type", "content-length", "content-disposition", "content-range", "accept-ranges"):
            self.assertIn(header, exposed)

    def test_upstream_error_exposes_only_sanitized_message(self):
        request = httpx.Request("POST", "https://private-upstream.example/v1/videos")
        response = httpx.Response(
            400,
            request=request,
            json={
                "error": {
                    "message": "Reference video duration must be between 2 and 15 seconds\nSee https://private-upstream.example/docs",
                    "internal_url": "https://api.pixellelabs.com/private",
                },
                "request_id": "upstream-secret-id",
            },
        )
        result = upstream_error(response)
        body = json.loads(result.body)
        top_level_result = upstream_error(httpx.Response(
            422,
            request=request,
            json={"error": "invalid_request", "message": "The image count exceeds 9"},
        ))
        self.assertEqual(result.status_code, 400)
        self.assertEqual(
            body["error"]["message"],
            "Reference video duration must be between 2 and 15 seconds See [redacted URL]",
        )
        self.assertNotIn("pixellelabs", json.dumps(body))
        self.assertNotIn("upstream-secret-id", json.dumps(body))
        self.assertEqual(
            json.loads(top_level_result.body)["error"]["message"],
            "The image count exceeds 9",
        )

    def test_failed_task_exposes_upstream_message_only_on_failure(self):
        task = {
            "task_id": "upstream-task",
            "model": "public-model",
            "protocol": "ark-v3",
            "created_at": 100,
        }
        failed, _ = normalize_task_payload(task, {
            "status": "failed",
            "error": {
                "code": "InvalidParameter",
                "message": "Invalid API key sk-upstreamSecret123 and Bearer private-token",
                "internal": "do not expose",
            },
        })
        processing, _ = normalize_task_payload(task, {
            "status": "running",
            "error": {"message": "transient internal warning"},
        })
        top_level_message, _ = normalize_task_payload(task, {
            "status": "failed",
            "message": "The selected resolution is not supported",
        })

        self.assertEqual(
            failed["error"]["message"],
            "Invalid API key sk-[redacted] and Bearer [redacted]",
        )
        self.assertEqual(failed["error"]["code"], "video_generation_failed")
        self.assertIsNone(processing["error"])
        self.assertEqual(
            top_level_message["error"]["message"],
            "The selected resolution is not supported",
        )

    def test_create_response_exposes_message_when_task_immediately_fails(self):
        response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://private-upstream.example/v1/videos"),
            json={
                "task_id": "immediate-failure-task",
                "status": "failed",
                "error": {"message": "Prompt violates the upstream policy"},
            },
        )

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, *_args, **_kwargs):
                return response

        with patch("app.proxy.httpx.AsyncClient", return_value=MockAsyncClient()):
            result = asyncio.run(create_video({"model": "audit-model", "prompt": "test"}, None))

        body = json.loads(result.body)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"], {
            "message": "Prompt violates the upstream policy",
            "code": "video_generation_failed",
        })

    def test_audit_data_is_correlated_and_encrypted(self):
        relay_request_id = database.start_audit_request(
            self.upstream["id"],
            "audit-model",
            "videos",
            {"model": "audit-model", "prompt": "private prompt"},
        )
        database.create_task(
            "upstream-audit-task",
            self.upstream["id"],
            relay_request_id,
            "audit-model",
            "videos",
            "queued",
        )
        database.record_audit_event(
            relay_request_id,
            "poll",
            200,
            '{"video_url":"https://api.pixellelabs.com/private.mp4"}',
            {"status": "completed"},
        )
        database.update_task(
            "upstream-audit-task",
            "completed",
            "https://api.pixellelabs.com/private.mp4",
            None,
        )
        self.assertTrue(database.set_public_task_id(relay_request_id, "task_public_123"))

        detail = database.get_audit_request(relay_request_id)
        self.assertIsNotNone(detail)
        self.assertEqual(detail["request_payload"]["prompt"], "private prompt")
        self.assertEqual(detail["source_video_url"], "https://api.pixellelabs.com/private.mp4")
        self.assertEqual(
            detail["sanitized_video_url"],
            "https://zl.yyapi.cloud/public/videos/task_public_123/content",
        )
        sanitized = detail["events"][0]["sanitized_body"]
        for field in ("url", "video_url", "result_url", "download_url"):
            self.assertEqual(sanitized[field], detail["sanitized_video_url"])
        self.assertIn("pixellelabs", detail["events"][0]["upstream_body"])

        with database.connection() as conn:
            row = conn.execute(
                "SELECT request_payload_encrypted, source_video_url_encrypted FROM audit_requests WHERE relay_request_id = ?",
                (relay_request_id,),
            ).fetchone()
            event = conn.execute(
                "SELECT upstream_body_encrypted FROM audit_events WHERE relay_request_id = ?",
                (relay_request_id,),
            ).fetchone()
        self.assertNotIn("private prompt", row["request_payload_encrypted"])
        self.assertNotIn("pixellelabs", row["source_video_url_encrypted"])
        self.assertNotIn("pixellelabs", event["upstream_body_encrypted"])

    def test_failed_audit_does_not_publish_content_link(self):
        relay_request_id = database.start_audit_request(
            self.upstream["id"],
            "audit-model",
            "videos",
            {"model": "audit-model", "prompt": "failed prompt"},
        )
        database.create_task(
            "upstream-failed-task",
            self.upstream["id"],
            relay_request_id,
            "audit-model",
            "videos",
            "queued",
        )
        database.record_audit_event(
            relay_request_id,
            "poll",
            200,
            '{"status":"failed","video_url":"https://private-upstream.example/content"}',
            {"status": "failed", "error": {"message": "Video generation failed"}},
        )
        database.update_task(
            "upstream-failed-task",
            "failed",
            "https://private-upstream.example/content",
            "Video generation failed",
        )
        self.assertTrue(database.set_public_task_id(relay_request_id, "task_public_failed"))

        detail = database.get_audit_request(relay_request_id)

        self.assertIsNone(detail["sanitized_video_url"])
        self.assertNotIn("video_url", detail["events"][0]["sanitized_body"])

    def test_create_response_sets_new_api_correlation_header(self):
        response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://private-upstream.example/v1/videos"),
            json={
                "id": "upstream-create-task",
                "task_id": "upstream-create-task",
                "status": "queued",
                "video_url": "https://api.pixellelabs.com/private-create.mp4",
            },
        )

        class MockAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return None

            async def post(self, *_args, **_kwargs):
                return response

        with patch("app.proxy.httpx.AsyncClient", return_value=MockAsyncClient()):
            result = asyncio.run(
                create_video(
                    {"model": "audit-model", "prompt": "test"},
                    None,
                    "task_public_create",
                )
            )

        relay_request_id = result.headers["X-Oneapi-Request-Id"]
        body = result.body.decode()
        self.assertTrue(relay_request_id.startswith("vrq_"))
        self.assertNotIn("task_id", json.loads(body))
        self.assertNotIn("pixellelabs", body)
        detail = database.get_audit_request(relay_request_id)
        self.assertEqual(detail["upstream_task_id"], "upstream-create-task")
        self.assertEqual(detail["public_task_id"], "task_public_create")
        self.assertIsNotNone(detail["public_download_expires_at"])
        self.assertIn("pixellelabs", detail["events"][0]["upstream_body"])


if __name__ == "__main__":
    unittest.main()
