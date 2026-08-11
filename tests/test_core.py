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
TEST_DATA_DIR = tempfile.TemporaryDirectory()
os.environ["DATA_DIR"] = TEST_DATA_DIR.name

from app import database, image_database
from app.config import settings
from app.main import app, normalize_discovered_models
from app.image_proxy import classify_health_outcome, forward_json
from app.model_profiles import capabilities_for, transform_create_payload
from app.proxy import create_video, fetch_task, normalize_status, normalize_task_payload, upstream_error
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
        self.assertTrue(public_url.startswith(f"{settings.public_base_url}/v1/images/assets/img_"))
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
            response = TestClient(app).get(f"/v1/images/assets/{asset_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"png-bytes")
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
        response = client.get("/admin/api/integration-document")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment; filename=\"video-api-integration.md\"", response.headers["content-disposition"])
        self.assertIn("stable-manxue", response.text)
        self.assertNotIn("manxue-900-10s", response.text)
        self.assertIn("/v1/videos", response.text)

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
                json={"public_base_url": "https://www.yyapi.cloud"},
                headers=headers,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["public_link_base_url"], "https://www.yyapi.cloud")
            dashboard = client.get("/admin/api/dashboard").json()
            self.assertEqual(dashboard["public_link_base_url"], "https://www.yyapi.cloud")
            self.assertEqual(
                dashboard["public_link_base_url_options"],
                ["https://www.yyapi.cloud", "https://zl.yyapi.cloud"],
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
                "https://www.yyapi.cloud/public/videos/query-domain-task/content",
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

    def test_status_mapping(self):
        expected = {
            "NOT_START": "queued",
            "IN_PROGRESS": "processing",
            "SUCCESS": "completed",
            "FAILURE": "failed",
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
                {"model": "", "upstream_model": "sora-v3-933-pro", "protocol": "videos", "profile": "default", "durations": []},
                {"model": "", "upstream_model": "seedance-2.0-fast", "protocol": "seedance", "profile": "default", "durations": []},
                {"model": "", "upstream_model": "veo31-fast", "protocol": "videos", "profile": "veo31-fast", "durations": []},
            ],
        )

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
        self.assertEqual(captured["payload"]["seconds"], 10)

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
                "seconds": 10,
                "resolution": "720p",
                "generate_audio": True,
                "image_url": "https://cdn/main.png",
                "reference_image_urls": ["https://cdn/ref.png"],
                "reference_videos": ["https://cdn/ref.mp4"],
                "audio_urls": ["https://cdn/voice.mp3"],
            },
        )
        self.assertEqual(capabilities_for("manxue-933", 10)["durations"], [10])

    def test_model_capabilities_are_public_and_cors_limited(self):
        response = TestClient(app).get(
            "/v1/model-capabilities",
            headers={"Origin": "https://image.yyapi.cloud"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://image.yyapi.cloud")
        self.assertTrue(any(item["id"] == "audit-model" for item in response.json()["data"]))

    def test_upstream_error_does_not_expose_provider_details(self):
        request = httpx.Request("POST", "https://private-upstream.example/v1/videos")
        response = httpx.Response(
            502,
            request=request,
            json={"error": {"message": "api.pixellelabs.com internal failure"}},
        )
        result = upstream_error(response)
        body = result.body.decode()
        self.assertEqual(result.status_code, 502)
        self.assertIn("Video upstream request failed", body)
        self.assertNotIn("pixellelabs", body)
        self.assertNotIn("private-upstream", body)

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
            result = asyncio.run(create_video({"model": "audit-model", "prompt": "test"}, None))

        relay_request_id = result.headers["X-Oneapi-Request-Id"]
        body = result.body.decode()
        self.assertTrue(relay_request_id.startswith("vrq_"))
        self.assertNotIn("task_id", json.loads(body))
        self.assertNotIn("pixellelabs", body)
        detail = database.get_audit_request(relay_request_id)
        self.assertEqual(detail["upstream_task_id"], "upstream-create-task")
        self.assertIn("pixellelabs", detail["events"][0]["upstream_body"])


if __name__ == "__main__":
    unittest.main()
