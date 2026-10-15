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
from fastapi.testclient import TestClient

from app import database, proxy
from app.channels import fuyao
from app.main import app
from app.proxy import create_video, fetch_task
from app.security import SESSION_COOKIE, create_session, csrf_token


def scripted_client(steps: list, calls: list):
    """An AsyncClient whose POSTs follow ``steps``: an exception, a response or a callable."""

    class ScriptedAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append(("POST", url, kwargs))
            step = steps.pop(0)
            if callable(step) and not isinstance(step, httpx.Response):
                step = await step()
            if isinstance(step, BaseException):
                raise step
            return step

        async def get(self, url, **kwargs):
            calls.append(("GET", url, kwargs))
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"id": url.rsplit("/", 1)[-1], "status": "in_progress", "progress": 40},
            )

    return ScriptedAsyncClient


def created(task_id: str) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("POST", "https://up.example/v1/videos"),
        json={"id": task_id, "status": "queued", "progress": 0},
    )


class CreateReliabilityTests(unittest.TestCase):
    def setUp(self):
        database.initialize()
        delays = patch.multiple(proxy, CREATE_CONNECT_RETRY_DELAY_SECONDS=0)
        delays.start()
        self.addCleanup(delays.stop)

    def _route(self, protocol: str = fuyao.PROTOCOL, base_url: str = "https://fuyao47.xyz") -> str:
        model = f"reliability-{time.time_ns()}"
        database.save_upstream({
            "name": model,
            "base_url": base_url,
            "api_key": "up-secret",
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": model,
                "upstream_model": "grok-imagine-video-1.5",
                "protocol": protocol,
                "profile": "fuyao-grok" if protocol == fuyao.PROTOCOL else "default",
                "image_count": 1,
                "video_count": 0,
                "audio_count": 0,
            }],
        })
        return model

    def _create(self, model: str, steps: list, calls: list, **kwargs):
        with patch("app.proxy.httpx.AsyncClient", scripted_client(steps, calls)):
            return asyncio.run(create_video({"model": model, "prompt": "p", "seconds": 6}, None, **kwargs))

    def _posts(self, calls: list) -> list:
        return [call for call in calls if call[0] == "POST"]

    def test_lost_response_is_never_resent(self):
        model = self._route()
        calls: list = []
        result = self._create(model, [httpx.ReadTimeout("lost")], calls)

        self.assertEqual(len(self._posts(calls)), 1)
        relay_request_id = result.headers["X-Oneapi-Request-Id"]
        self.assertEqual(json.loads(result.body)["id"], relay_request_id)
        audit = database.get_audit_request(relay_request_id)
        self.assertEqual(audit["create_state"], "unknown")
        self.assertEqual(audit["events"][0]["upstream_body"], "ReadTimeout: lost")

        # Neither recovery nor polling sends the request again.
        with patch("app.proxy.httpx.AsyncClient", scripted_client([], calls)):
            proxy.recover_create(relay_request_id)
            proxy.recover_pending_creates()
            asyncio.run(fetch_task(relay_request_id))
        self.assertEqual(len(self._posts(calls)), 1)
        self.assertEqual(database.get_audit_request(relay_request_id)["create_state"], "unknown")

    def test_unconfirmed_create_never_reports_failure(self):
        model = self._route()
        calls: list = []
        result = self._create(model, [httpx.RemoteProtocolError("closed")], calls)

        self.assertEqual(result.status_code, 200)
        body = json.loads(result.body)
        relay_request_id = result.headers["X-Oneapi-Request-Id"]
        self.assertEqual(body["id"], relay_request_id)
        self.assertEqual(body["status"], "queued")
        audit = database.get_audit_request(relay_request_id)
        self.assertEqual(audit["status"], "queued")
        self.assertEqual(audit["create_state"], "unknown")
        self.assertEqual(audit["events"][0]["http_status"], None)
        self.assertIn("RemoteProtocolError", audit["events"][0]["upstream_body"])

        # Polling the provisional ID stays pending instead of failing.
        polled = asyncio.run(fetch_task(relay_request_id))
        self.assertEqual(json.loads(polled.body)["status"], "queued")

    def test_gateway_timeout_after_the_request_arrived_is_unconfirmed(self):
        model = self._route()
        calls: list = []
        timeout = httpx.Response(524, request=httpx.Request("POST", "https://fuyao47.xyz/v1/videos"), text="timeout")
        result = self._create(model, [timeout], calls)

        self.assertEqual(result.status_code, 200)
        audit = database.get_audit_request(result.headers["X-Oneapi-Request-Id"])
        self.assertEqual(audit["status"], "queued")
        self.assertEqual(audit["create_state"], "unknown")
        self.assertEqual(audit["events"][0]["http_status"], 524)
        self.assertEqual(len(self._posts(calls)), 1)

    def test_refused_connection_is_retried_then_failed(self):
        model = self._route()
        calls: list = []
        refused = [httpx.ConnectError("refused") for _ in range(proxy.CREATE_CONNECT_ATTEMPTS)]
        with self.assertRaises(HTTPException) as raised:
            self._create(model, refused, calls)

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(len(calls), proxy.CREATE_CONNECT_ATTEMPTS)
        relay_request_id = raised.exception.headers["X-Oneapi-Request-Id"]
        audit = database.get_audit_request(relay_request_id)
        self.assertEqual(audit["status"], "failed")
        self.assertIsNone(audit["create_state"])

    def test_refused_connection_recovers_on_retry(self):
        model = self._route()
        task_id = f"task_{time.time_ns()}"
        calls: list = []
        result = self._create(model, [httpx.ConnectError("refused"), created(task_id)], calls)
        self.assertEqual(json.loads(result.body)["id"], task_id)

    def test_definite_upstream_rejection_still_fails(self):
        model = self._route()
        calls: list = []
        rejected = httpx.Response(
            400,
            request=httpx.Request("POST", "https://fuyao47.xyz/v1/videos"),
            json={"error": {"message": "bad prompt"}},
        )
        result = self._create(model, [rejected], calls)
        self.assertEqual(result.status_code, 400)
        audit = database.get_audit_request(result.headers["X-Oneapi-Request-Id"])
        self.assertEqual(audit["status"], "failed")
        self.assertIsNone(audit["create_state"])

    def test_non_idempotent_upstream_is_not_resent(self):
        model = self._route(protocol="o10-grok", base_url="https://o10.example")
        calls: list = []
        result = self._create(model, [httpx.ReadTimeout("lost")], calls)

        self.assertEqual(len(self._posts(calls)), 1)
        relay_request_id = result.headers["X-Oneapi-Request-Id"]
        self.assertEqual(json.loads(result.body)["id"], relay_request_id)
        self.assertEqual(database.get_audit_request(relay_request_id)["create_state"], "unknown")

    def test_operator_binds_the_upstream_task_and_the_provisional_id_follows_it(self):
        model = self._route(protocol="o10-grok", base_url="https://o10.example")
        calls: list = []
        result = self._create(model, [httpx.ReadTimeout("lost")], calls)
        relay_request_id = result.headers["X-Oneapi-Request-Id"]
        upstream_task_id = f"o10_{time.time_ns()}"

        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        bound = client.put(
            f"/admin/api/tasks/{relay_request_id}/upstream-task",
            headers={"X-CSRF-Token": csrf_token(session)},
            json={"upstream_task_id": upstream_task_id},
        )
        self.assertEqual(bound.status_code, 200)
        self.assertEqual(bound.json()["upstream_task_id"], upstream_task_id)
        self.assertIsNone(bound.json()["create_state"])

        with patch("app.proxy.httpx.AsyncClient", scripted_client([], calls)):
            polled = asyncio.run(fetch_task(relay_request_id))
        self.assertEqual(json.loads(polled.body)["status"], "processing")
        self.assertTrue(calls[-1][1].endswith(f"/{upstream_task_id}"))

        again = client.put(
            f"/admin/api/tasks/{relay_request_id}/upstream-task",
            headers={"X-CSRF-Token": csrf_token(session)},
            json={"upstream_task_id": "other"},
        )
        self.assertEqual(again.status_code, 409)

    def test_operator_confirms_nothing_was_created(self):
        model = self._route(protocol="o10-grok", base_url="https://o10.example")
        calls: list = []
        result = self._create(model, [httpx.ReadTimeout("lost")], calls)
        relay_request_id = result.headers["X-Oneapi-Request-Id"]

        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        failed = client.post(
            f"/admin/api/tasks/{relay_request_id}/fail-create",
            headers={"X-CSRF-Token": csrf_token(session)},
        )
        self.assertEqual(failed.status_code, 200)
        self.assertEqual(failed.json()["status"], "failed")

        polled = asyncio.run(fetch_task(relay_request_id))
        self.assertEqual(json.loads(polled.body)["status"], "failed")

    def test_slow_upstream_gets_a_provisional_id_and_finishes_in_the_background(self):
        model = self._route()
        task_id = f"task_{time.time_ns()}"
        calls: list = []

        async def slow():
            await asyncio.sleep(0.3)
            return created(task_id)

        async def scenario():
            with patch("app.proxy.httpx.AsyncClient", scripted_client([slow], calls)):
                first = await create_video({"model": model, "prompt": "p", "seconds": 6}, None)
                relay_request_id = first.headers["X-Oneapi-Request-Id"]
                await proxy._create_jobs[relay_request_id]
                return first, relay_request_id

        with patch("app.proxy.settings", _settings_with(video_create_wait_seconds=0.05)):
            first, relay_request_id = asyncio.run(scenario())

        self.assertEqual(json.loads(first.body)["id"], relay_request_id)
        audit = database.get_audit_request(relay_request_id)
        self.assertEqual(audit["upstream_task_id"], task_id)
        with patch("app.proxy.httpx.AsyncClient", scripted_client([], calls)):
            polled = asyncio.run(fetch_task(relay_request_id))
        self.assertEqual(json.loads(polled.body)["status"], "processing")

    def test_orphaned_submission_is_parked_not_resent(self):
        model = self._route()
        calls: list = []
        result = self._create(model, [httpx.ReadTimeout("lost")], calls)
        relay_request_id = result.headers["X-Oneapi-Request-Id"]
        # Simulate a restart mid-POST: the row says submitting, nobody owns it.
        with database.connection() as conn:
            conn.execute(
                "UPDATE audit_requests SET create_state = 'submitting', create_started_at = 0 WHERE relay_request_id = ?",
                (relay_request_id,),
            )

        with patch("app.proxy.httpx.AsyncClient", scripted_client([], calls)):
            proxy.recover_create(relay_request_id)
        self.assertEqual(len(self._posts(calls)), 1)
        self.assertEqual(database.get_audit_request(relay_request_id)["create_state"], "unknown")

    def test_failed_request_cannot_be_bound(self):
        model = self._route()
        calls: list = []
        with self.assertRaises(HTTPException) as raised:
            self._create(model, [httpx.ConnectError("refused") for _ in range(proxy.CREATE_CONNECT_ATTEMPTS)], calls)
        relay_request_id = raised.exception.headers["X-Oneapi-Request-Id"]
        with self.assertRaises(ValueError):
            proxy.bind_upstream_task(relay_request_id, f"task_{time.time_ns()}")
        self.assertEqual(database.get_audit_request(relay_request_id)["status"], "failed")

    def test_sweep_skips_requests_that_only_wait_for_an_operator(self):
        model = self._route()
        calls: list = []
        result = self._create(model, [httpx.ReadTimeout("lost")], calls)
        relay_request_id = result.headers["X-Oneapi-Request-Id"]
        now = int(time.time())
        self.assertNotIn(relay_request_id, database.list_pending_create_ids(now, now - 3600, 50))
        self.assertIn(relay_request_id, database.list_pending_create_ids(now, now + 1, 50))

    def test_in_flight_submission_is_left_alone(self):
        model = self._route()
        calls: list = []
        result = self._create(model, [httpx.ReadTimeout("lost")], calls)
        relay_request_id = result.headers["X-Oneapi-Request-Id"]
        with database.connection() as conn:
            conn.execute(
                "UPDATE audit_requests SET create_state = 'submitting', create_started_at = ? WHERE relay_request_id = ?",
                (int(time.time()), relay_request_id),
            )
        proxy.recover_create(relay_request_id)
        self.assertEqual(database.get_audit_request(relay_request_id)["create_state"], "submitting")

    def test_unconfirmed_create_expires_without_any_resend(self):
        model = self._route()
        calls: list = []
        result = self._create(model, [httpx.ReadTimeout("lost")], calls)
        relay_request_id = result.headers["X-Oneapi-Request-Id"]
        with database.connection() as conn:
            conn.execute(
                "UPDATE audit_requests SET created_at = 0 WHERE relay_request_id = ?",
                (relay_request_id,),
            )

        with patch("app.proxy.httpx.AsyncClient", scripted_client([], calls)):
            proxy.recover_create(relay_request_id)
        self.assertEqual(len(self._posts(calls)), 1)
        audit = database.get_audit_request(relay_request_id)
        self.assertEqual(audit["status"], "failed")
        self.assertIsNone(audit["create_state"])


def _settings_with(**overrides):
    from dataclasses import replace

    return replace(proxy.settings, **overrides)


if __name__ == "__main__":
    unittest.main()
