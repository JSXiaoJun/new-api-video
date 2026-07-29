from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
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

from app import database
from app.main import app, normalize_discovered_models
from app.proxy import create_video, normalize_status, normalize_task_payload, upstream_error
from app.security import SESSION_COOKIE, create_session, csrf_token, read_session, secret_box
from fastapi.testclient import TestClient


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()
        cls.upstream = database.save_upstream(
            {
                "name": "audit-upstream",
                "base_url": "https://private-upstream.example",
                "api_key": "private-key",
                "enabled": True,
                "priority": 1,
                "routes": [{"model": "audit-model", "protocol": "videos"}],
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
        session = create_session("admin")
        self.assertEqual(read_session(session)["username"], "admin")
        encrypted = secret_box.encrypt("sk-secret")
        self.assertNotIn("sk-secret", encrypted)
        self.assertEqual(secret_box.decrypt(encrypted), "sk-secret")

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
                {"model": "sora-v3-933-pro", "protocol": "videos"},
                {"model": "seedance-2.0-fast", "protocol": "seedance"},
                {"model": "veo31-fast", "protocol": "videos"},
            ],
        )

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
            "https://zl.yyapi.cloud/v1/videos/task_public_123/content",
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
