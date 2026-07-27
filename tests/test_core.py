from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import httpx

os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-with-more-than-32-chars")
os.environ.setdefault("ADAPTER_API_KEY", "test-adapter-key")
os.environ.setdefault("ENCRYPTION_KEY", "IougsRYbjtzQcNSrzLV2O-TQ3k1PDP69XcfdR3Lxp3I=")

from app import database
from app.main import app, normalize_discovered_models
from app.proxy import normalize_status, normalize_task_payload, upstream_error
from app.security import create_session, read_session, secret_box
from fastapi.testclient import TestClient


class CoreTests(unittest.TestCase):
    def test_admin_page_redirects_to_login_without_session(self):
        response = TestClient(app).get("/admin", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/admin/login")

    def test_adapter_endpoint_requires_channel_key(self):
        response = TestClient(app).get("/v1/models")
        self.assertEqual(response.status_code, 401)

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


if __name__ == "__main__":
    unittest.main()
