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

from app import database, proxy
from app.channels import fuyao
from app.proxy import fetch_task


def scripted_gets(steps: list, calls: list):
    """An AsyncClient whose GETs follow ``steps``: an exception or a response."""

    class ScriptedAsyncClient:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs.get("timeout")))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            calls.append(("GET", url))
            step = steps.pop(0)
            if isinstance(step, BaseException):
                raise step
            return step

    return ScriptedAsyncClient


def task_answer(task_id: str, status: str) -> httpx.Response:
    url = f"https://fuyao47.xyz/v1/videos/{task_id}"
    return httpx.Response(
        200,
        request=httpx.Request("GET", url),
        json={"id": task_id, "status": status, "progress": 100 if status == "completed" else 40},
    )


class PollReliabilityTests(unittest.TestCase):
    def setUp(self):
        database.initialize()
        delays = patch.multiple(proxy, POLL_RETRY_DELAY_SECONDS=0)
        delays.start()
        self.addCleanup(delays.stop)

    def _task(self, status: str = "queued") -> tuple[str, str]:
        model = f"poll-{time.time_ns()}"
        upstream = database.save_upstream({
            "name": model,
            "base_url": "https://fuyao47.xyz",
            "api_key": "fuyao-secret",
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": model,
                "upstream_model": "wan3.0-video",
                "protocol": fuyao.PROTOCOL,
                "profile": "fuyao-wan3",
                "image_count": 1,
                "video_count": 1,
                "audio_count": 0,
            }],
        })
        relay_request_id = database.start_audit_request(upstream["id"], model, fuyao.PROTOCOL, {"model": model})
        task_id = f"task_compat_{time.time_ns()}"
        database.create_task(task_id, upstream["id"], relay_request_id, model, fuyao.PROTOCOL, status)
        return task_id, relay_request_id

    def _poll(self, task_id: str, steps: list, calls: list):
        with patch("app.proxy.httpx.AsyncClient", scripted_gets(steps, calls)):
            return asyncio.run(fetch_task(task_id))

    def test_a_hanging_read_is_retried_and_the_next_answer_is_used(self):
        task_id, _ = self._task()
        calls: list = []
        result = self._poll(task_id, [httpx.ReadTimeout(""), task_answer(task_id, "completed")], calls)

        self.assertEqual(result.status_code, 200)
        self.assertEqual(json.loads(result.body)["status"], "completed")
        self.assertEqual(len([call for call in calls if call[0] == "GET"]), 2)
        self.assertEqual(database.get_task(task_id)["status"], "completed")

    def test_each_attempt_is_bounded(self):
        task_id, _ = self._task()
        calls: list = []
        self._poll(task_id, [task_answer(task_id, "processing")], calls)
        timeouts = [call[1] for call in calls if call[0] == "client"]
        self.assertTrue(all(timeout <= proxy.POLL_ATTEMPT_TIMEOUT_SECONDS for timeout in timeouts))

    def test_an_unreachable_upstream_reports_pending_never_a_failure(self):
        task_id, relay_request_id = self._task("processing")
        calls: list = []
        lost = [httpx.ReadTimeout("") for _ in range(proxy.POLL_ATTEMPTS)]
        result = self._poll(task_id, lost, calls)

        self.assertEqual(result.status_code, 200)
        body = json.loads(result.body)
        self.assertEqual(body["status"], "processing")
        self.assertIsNone(body["error"])
        self.assertEqual(result.headers["X-Oneapi-Request-Id"], relay_request_id)
        self.assertEqual(len([call for call in calls if call[0] == "GET"]), proxy.POLL_ATTEMPTS)
        self.assertEqual(database.get_task(task_id)["status"], "processing")
        event = database.get_audit_request(relay_request_id)["events"][0]
        self.assertEqual(event["upstream_body"], "ReadTimeout")
        self.assertEqual(event["sanitized_body"]["detail"], proxy.POLL_UNREACHABLE_DETAIL)

    def test_a_settled_task_keeps_its_answer_when_the_upstream_is_unreachable(self):
        task_id, _ = self._task("completed")
        calls: list = []
        lost = [httpx.ConnectError("refused") for _ in range(proxy.POLL_ATTEMPTS)]
        result = self._poll(task_id, lost, calls)

        self.assertEqual(json.loads(result.body)["status"], "completed")
        self.assertEqual(database.get_task(task_id)["status"], "completed")

    def test_a_gateway_page_with_a_2xx_status_keeps_the_task_pending(self):
        task_id, _ = self._task()
        calls: list = []
        page = httpx.Response(200, request=httpx.Request("GET", "https://fuyao47.xyz"), text="<html>busy</html>")
        result = self._poll(task_id, [page], calls)

        self.assertEqual(result.status_code, 200)
        self.assertEqual(json.loads(result.body)["status"], "queued")

    def test_a_definite_not_found_still_fails_the_task(self):
        task_id, _ = self._task()
        calls: list = []
        missing = httpx.Response(
            404,
            request=httpx.Request("GET", "https://fuyao47.xyz"),
            json={"error": {"message": "task not found"}},
        )
        result = self._poll(task_id, [missing], calls)

        self.assertEqual(result.status_code, 404)
        self.assertEqual(database.get_task(task_id)["status"], "failed")

    def test_download_lookup_retries_a_hanging_content_endpoint(self):
        task_id, _ = self._task("completed")
        attempts: list = []
        signed = "https://oss.example/v.mp4?Expires=1"

        class ContentClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def stream(self, method, url, **_kwargs):
                attempts.append(url)

                class Ctx:
                    async def __aenter__(self_inner):
                        if len(attempts) == 1:
                            raise httpx.ReadTimeout("")
                        return httpx.Response(200, json={"id": task_id, "url": signed})

                    async def __aexit__(self_inner, *_args):
                        return None

                return Ctx()

        task = database.get_task(task_id)
        with patch("app.proxy.httpx.AsyncClient", ContentClient):
            link = asyncio.run(proxy._resolve_fuyao_video_link(f"https://fuyao47.xyz/v1/videos/{task_id}/content", task))
        self.assertEqual(link, signed)
        self.assertEqual(len(attempts), 2)

    def test_download_lookup_gives_up_after_every_attempt_hangs(self):
        task_id, _ = self._task("completed")

        class HangingClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            def stream(self, *_args, **_kwargs):
                class Ctx:
                    async def __aenter__(self_inner):
                        raise httpx.ReadTimeout("")

                    async def __aexit__(self_inner, *_args):
                        return None

                return Ctx()

        task = database.get_task(task_id)
        with patch("app.proxy.httpx.AsyncClient", HangingClient):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(proxy._resolve_fuyao_video_link("https://fuyao47.xyz/v1/videos/x/content", task))
        self.assertEqual(raised.exception.status_code, 502)

    def test_console_refresh_runs_in_the_background_one_sweep_at_a_time(self):
        started: list = []

        async def slow_sweep():
            started.append(1)
            await asyncio.sleep(0.05)

        async def scenario():
            with patch("app.proxy.reconcile_pending_tasks", slow_sweep):
                proxy.schedule_reconcile()
                proxy.schedule_reconcile()
                await proxy._reconcile_job

        asyncio.run(scenario())
        self.assertEqual(started, [1])


if __name__ == "__main__":
    unittest.main()
