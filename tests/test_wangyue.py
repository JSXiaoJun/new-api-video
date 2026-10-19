from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

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

from app import database, proxy
from app.channels import wangyue
from app.channels.wangyue import assets, signing
from app.main import app
from app.model_profiles import capabilities_for
from app.proxy import create_video, fetch_task, stream_content
from app.security import SESSION_COOKIE, create_session, csrf_token


BASE_URL = "https://1456719883-78ndn27wdv.ap-guangzhou.tencentscf.com"
API_KEY = "kid-test:secret-test"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

# Reference values computed by the partner guide's own Node ``sign()`` for the
# same body (Node v22): the Python signer must produce them byte for byte.
NODE_BODY = {
    "type": "doubao_video",
    "taskId": "11111111-1111-4111-8111-111111111111",
    "idempotencyKey": "partner-task-20261018-0001",
    "payload": {
        "projectId": "p",
        "rowId": "r",
        # Quotes, a backslash, an emoji, control characters and U+2028.
        "prompt": "雨夜霓虹街道，镜头缓慢推进 \"引号\" " + chr(92) + " " + chr(0x1F600) + chr(10)
                  + "换行" + chr(9) + "<>&" + chr(0x2028) + chr(0xE9),
        "referenceImageAssetIds": ["b", "a"],
        "settings": {
            "model": "seedance2.0fast", "duration": 15, "channel": "doubao", "aspectRatio": "16:9",
            "resolution": "720P", "flag": True, "ratio": 0.5,
        },
    },
}
NODE_BODY_SHA256 = "7fffc6ff635fadef515262f9a2505ccebe0e9c0740dbeb6a0698e9b658527771"
NODE_POST_SIGNATURE = "ca0b5086c19679aa3a5e32ec363283ca58c4f290ebfbb9ec266fd7c499090784"
NODE_GET_SIGNATURE = "32aa29ef4ce0b4663b97ffbd130cb7c728c4c585569cb73a0a7bb96d53bbe88a"


def _response(method: str, url: str, status: int = 200, body=None, content: bytes | None = None, headers=None):
    request = httpx.Request(method, url)
    if content is not None:
        return httpx.Response(status, request=request, content=content, headers=headers)
    return httpx.Response(status, request=request, json=body, headers=headers)


def fake_client(calls: list, routes: dict):
    """An AsyncClient answering by ``(METHOD, path)``; a value may be a callable or a list."""

    def answer(method: str, url: str, kwargs: dict):
        calls.append((method, url, kwargs))
        key = (method, urlsplit(url).path)
        step = routes.get(key, routes.get((method, urlsplit(url).netloc)))
        if isinstance(step, list):
            step = step.pop(0)
        if callable(step):
            step = step(url, kwargs)
        if isinstance(step, BaseException):
            raise step
        if step is None:
            raise AssertionError(f"unexpected {method} {url}")
        return step

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            return answer("GET", url, kwargs)

        async def post(self, url, **kwargs):
            return answer("POST", url, kwargs)

        async def put(self, url, **kwargs):
            return answer("PUT", url, kwargs)

        def stream(self, method, url, **kwargs):
            response = answer(method, url, kwargs)

            class Ctx:
                async def __aenter__(self_inner):
                    return response

                async def __aexit__(self_inner, *_args):
                    return None

            return Ctx()

    return FakeAsyncClient


async def public_dns(host, port, **_kwargs):
    return [(2, 1, 6, "", ("93.184.216.34", port))]


def assert_signed(test: unittest.TestCase, method: str, url: str, kwargs: dict, idempotency_key: str = ""):
    headers = kwargs["headers"]
    test.assertNotIn("Authorization", headers)
    test.assertEqual(headers[signing.KEY_ID_HEADER], "kid-test")
    body = kwargs.get("content", signing.EMPTY_BODY)
    expected = signing.sign(
        "secret-test", method, urlsplit(url).path, int(headers[signing.TIMESTAMP_HEADER]),
        headers[signing.NONCE_HEADER], idempotency_key, body,
    )
    test.assertEqual(headers[signing.SIGNATURE_HEADER], expected)


def task_answer(task_id: str, status: str, **extra) -> dict:
    return {"ok": True, "task": {"id": task_id, "status": status, "type": "doubao_video", **extra}}


class WangyueSigningTests(unittest.TestCase):
    def test_matches_the_guides_node_reference(self):
        body = signing.encode_body(NODE_BODY)
        self.assertEqual(hashlib.sha256(body).hexdigest(), NODE_BODY_SHA256)
        self.assertEqual(
            signing.sign("test-secret", "post", "/v1/pool/api/tasks", 1776936222,
                         "abcdef0123456789abcd", "partner-task-20261018-0001", body),
            NODE_POST_SIGNATURE,
        )
        self.assertEqual(
            signing.sign("test-secret", "GET", "/v1/pool/api/tasks/x", 1776936222,
                         "abcdef0123456789abcd", "", signing.encode_body(None)),
            NODE_GET_SIGNATURE,
        )

    def test_stable_json_matches_javascript_number_printing(self):
        self.assertEqual(signing.stable_json({"b": 15.0, "a": [2, 1]}), '{"a":[2,1],"b":15}')

    def test_credentials_must_be_key_id_and_secret(self):
        self.assertEqual(signing.parse_credentials(" kid : sec:ret "), ("kid", "sec:ret"))
        for bad in ("", "kid-only", ":secret", "kid:"):
            with self.assertRaises(signing.WangyueCredentialError):
                signing.parse_credentials(bad)

    def test_every_request_gets_a_valid_unique_nonce(self):
        first = signing.signed_headers(API_KEY, "GET", BASE_URL + "/v1/pool/api/tasks/a")
        second = signing.signed_headers(API_KEY, "GET", BASE_URL + "/v1/pool/api/tasks/a")
        self.assertNotEqual(first[signing.NONCE_HEADER], second[signing.NONCE_HEADER])
        self.assertRegex(first[signing.NONCE_HEADER], r"^[A-Za-z0-9_-]{16,96}$")
        self.assertNotIn(signing.IDEMPOTENCY_HEADER, first)
        self.assertNotIn("secret-test", json.dumps(first))

    def test_query_strings_are_refused(self):
        with self.assertRaises(ValueError):
            signing.signed_headers(API_KEY, "GET", BASE_URL + "/v1/pool/api/tasks?limit=1")


class WangyueCatalogTests(unittest.TestCase):
    def test_base_url_detection_is_host_exact(self):
        self.assertTrue(wangyue.is_wangyue_base_url(BASE_URL))
        self.assertTrue(wangyue.is_wangyue_base_url("https://developers.wangyueaigc.cn"))
        self.assertFalse(wangyue.is_wangyue_base_url("https://other.ap-guangzhou.tencentscf.com"))
        self.assertFalse(wangyue.is_wangyue_base_url("https://wangyueaigc.cn.evil.example"))

    def test_published_models_map_to_their_families(self):
        self.assertEqual(wangyue.suggest_route("seedance2.0fast")["profile"], "wangyue-seedance-fast")
        self.assertEqual(wangyue.suggest_route("seedance2.0mini")["profile"], "wangyue-seedance-mini")
        self.assertEqual(wangyue.suggest_route("vidu-q3")["profile"], "wangyue-vidu-q3")
        self.assertEqual(wangyue.suggest_route("seedance2.0fast")["durations"], [10, 15])
        self.assertEqual(wangyue.suggest_route("seedance2.0mini")["durations"], [15])

    def test_capabilities_advertise_images_only(self):
        capabilities = capabilities_for("wangyue-seedance-fast", None, 9, 0, 0)
        self.assertEqual(capabilities["maxImages"], 9)
        self.assertEqual(capabilities["maxVideos"], 0)
        self.assertEqual(capabilities["maxAudios"], 0)
        self.assertFalse(capabilities["referenceVideo"])
        self.assertEqual(capabilities["resolutions"], ["720p"])

    def test_discovered_models_read_model_ids(self):
        self.assertEqual(
            wangyue.discovered_models({"ok": True, "models": [
                {"modelId": "seedance2.0fast", "metadata": {"capability": "video"}},
                {"modelId": "some-chat", "metadata": {"capability": "chat"}},
                {"displayName": "no id"},
            ]}),
            [{"id": "seedance2.0fast"}],
        )


RELAY_ID = "vrq_0123456789abcdef0123456789abcdef"


def build(payload: dict, profile: str, durations: list[int] | None = None):
    return wangyue.transform_create_payload(payload, profile, RELAY_ID, "key-12345678", durations)


class WangyuePayloadTests(unittest.TestCase):
    def test_text_to_video_body(self):
        body, images = build(
            {"model": "seedance2.0fast", "prompt": " 雨夜 ", "duration": 15, "aspect_ratio": "9:16"},
            "wangyue-seedance-fast",
        )
        self.assertEqual(images, [])
        self.assertEqual(body, {
            "type": "doubao_video",
            "taskId": "01234567-89ab-cdef-0123-456789abcdef",
            "idempotencyKey": "key-12345678",
            "payload": {
                "projectId": "new-api-video",
                "rowId": RELAY_ID,
                "prompt": "雨夜",
                "settings": {
                    "model": "seedance2.0fast", "channel": "doubao", "duration": 15,
                    "aspectRatio": "9:16", "resolution": "720P",
                },
            },
        })

    def test_fast_needs_an_explicit_duration_unless_the_route_pins_one(self):
        with self.assertRaisesRegex(wangyue.WangyueRequestError, "duration"):
            build({"model": "seedance2.0fast", "prompt": "p"}, "wangyue-seedance-fast")
        body, _ = build({"model": "seedance2.0fast", "prompt": "p"}, "wangyue-seedance-fast", [10])
        self.assertEqual(body["payload"]["settings"]["duration"], 10)
        body, _ = build({"model": "seedance2.0fast", "prompt": "p", "seconds": "10"}, "wangyue-seedance-fast")
        self.assertEqual(body["payload"]["settings"]["duration"], 10)

    def test_mini_defaults_to_its_only_duration_and_rejects_others(self):
        body, _ = build({"model": "seedance2.0mini", "prompt": "p"}, "wangyue-seedance-mini")
        self.assertEqual(body["payload"]["settings"]["duration"], 15)
        with self.assertRaises(wangyue.WangyueRequestError):
            build({"model": "seedance2.0mini", "prompt": "p", "duration": 10}, "wangyue-seedance-mini")
        with self.assertRaises(wangyue.WangyueRequestError):
            build({"model": "seedance2.0mini", "prompt": "p", "duration": 15, "seconds": 10}, "wangyue-seedance-mini")

    def test_reference_modes(self):
        body, images = build(
            {"model": "seedance2.0mini", "prompt": "p", "image_urls": ["https://a/1.png", "https://a/2.png"]},
            "wangyue-seedance-mini",
        )
        self.assertEqual(body["payload"]["settings"]["referenceMode"], "auto")
        self.assertEqual(images, ["https://a/1.png", "https://a/2.png"])

        body, images = build(
            {"model": "seedance2.0mini", "prompt": "p", "first_frame_url": "https://a/f.png"},
            "wangyue-seedance-mini",
        )
        self.assertEqual(body["payload"]["settings"]["referenceMode"], "start_frame")

        body, images = build(
            {"model": "seedance2.0mini", "prompt": "p",
             "last_frame_url": "https://a/l.png", "first_frame_url": "https://a/f.png"},
            "wangyue-seedance-mini",
        )
        self.assertEqual(body["payload"]["settings"]["referenceMode"], "start_end")
        self.assertEqual(images, ["https://a/f.png", "https://a/l.png"])

        body, _ = build(
            {"model": "seedance2.0mini", "prompt": "p", "reference_mode": "start_frame",
             "image_urls": ["https://a/1.png"]},
            "wangyue-seedance-mini",
        )
        self.assertEqual(body["payload"]["settings"]["referenceMode"], "start_frame")

    def test_unserviceable_image_requests_are_rejected(self):
        cases = [
            {"first_frame_url": "https://a/f.png", "image_urls": ["https://a/1.png"]},
            {"last_frame_url": "https://a/l.png"},
            {"reference_mode": "start_end", "image_urls": ["https://a/1.png"]},
            {"reference_mode": "start_frame"},
            {"reference_mode": "magic", "image_urls": ["https://a/1.png"]},
            {"image_urls": [f"https://a/{index}.png" for index in range(10)]},
        ]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(wangyue.WangyueRequestError):
                build({"model": "seedance2.0mini", "prompt": "p", **extra}, "wangyue-seedance-mini")

    def test_ratio_and_resolution_are_checked(self):
        body, _ = build({"model": "seedance2.0mini", "prompt": "p", "size": "1280x720"}, "wangyue-seedance-mini")
        self.assertEqual(body["payload"]["settings"]["aspectRatio"], "16:9")
        for extra in ({"aspect_ratio": "21:9"}, {"resolution": "1080p"}):
            with self.subTest(extra=extra), self.assertRaises(wangyue.WangyueRequestError):
                build({"model": "seedance2.0mini", "prompt": "p", **extra}, "wangyue-seedance-mini")

    def test_vidu_uses_its_channel_and_one_video(self):
        body, images = build(
            {"model": "vidu-q3", "prompt": "p", "duration": 8, "resolution": "720p",
             "image_urls": ["https://a/1.png"]},
            "wangyue-vidu-q3",
        )
        settings = body["payload"]["settings"]
        self.assertEqual(body["type"], "pool_video")
        self.assertEqual(settings["channel"], "vidu")
        self.assertEqual(settings["resolution"], "720P")
        self.assertEqual(settings["referenceMode"], "image_reference")
        self.assertEqual(settings["quantity"], 1)
        with self.assertRaises(wangyue.WangyueRequestError):
            build({"model": "vidu-q3", "prompt": "p", "duration": 8, "first_frame_url": "https://a/f.png"},
                  "wangyue-vidu-q3")

    def test_idempotency_keys_fit_the_gateway_rules(self):
        self.assertEqual(wangyue.normalize_idempotency_key("partner-task-0001"), "partner-task-0001")
        hashed = wangyue.normalize_idempotency_key("短")
        self.assertRegex(hashed, r"^nav-[0-9a-f]{32}$")
        self.assertEqual(hashed, wangyue.normalize_idempotency_key("短"))


class WangyueTaskParsingTests(unittest.TestCase):
    def test_statuses(self):
        cases = {
            "queued": ("queued", 0), "leased": ("processing", 50), "review_required": ("processing", 50),
            "completed": ("completed", None), "failed": ("failed", None), "cancelled": ("failed", None),
        }
        for raw, (status, progress) in cases.items():
            fields = wangyue.extract_task_fields(task_answer("t1", raw), "t1")
            self.assertEqual((fields["status"], fields["progress"]), (status, progress), raw)
        completed = wangyue.extract_task_fields(task_answer("t1", "completed"), "t1")
        self.assertEqual(completed["video_url"], "/v1/pool/api/tasks/t1/result")
        cancelled = wangyue.extract_task_fields(task_answer("t1", "cancelled"), "t1")
        self.assertEqual(cancelled["error"], "任务已取消")

    def test_credential_rejections_are_recognised(self):
        self.assertTrue(wangyue.is_credential_rejection(401, None))
        self.assertTrue(wangyue.is_credential_rejection(400, {"error": "api_key_id_invalid"}))
        self.assertTrue(wangyue.is_credential_rejection(409, {"error": "pool_api_replay"}))
        self.assertFalse(wangyue.is_credential_rejection(400, {"error": "request_json_invalid"}))
        self.assertFalse(wangyue.is_credential_rejection(404, {"error": "pool_api_task_not_found"}))


class WangyueFlowTests(unittest.TestCase):
    def setUp(self):
        database.initialize()
        patches = [
            patch.multiple(proxy, CREATE_CONNECT_RETRY_DELAY_SECONDS=0, POLL_RETRY_DELAY_SECONDS=0),
            patch.object(assets, "RETRY_DELAY_SECONDS", 0),
            patch("app.media_probe._resolve", public_dns),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def _route(self, upstream_model: str = "seedance2.0fast", api_key: str = API_KEY) -> str:
        public_model = f"wy-{time.time_ns()}"
        route = wangyue.suggest_route(upstream_model)
        database.save_upstream({
            "name": public_model,
            "base_url": BASE_URL,
            "api_key": api_key,
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": public_model,
                "upstream_model": upstream_model,
                "protocol": wangyue.PROTOCOL,
                "profile": route["profile"],
                "durations": route["durations"],
                "resolutions": route["resolutions"],
                "image_count": 9,
                "video_count": 0,
                "audio_count": 0,
            }],
        })
        return public_model

    def _create(self, payload: dict, routes: dict, calls: list, key: str | None = None):
        with patch("app.proxy.httpx.AsyncClient", fake_client(calls, routes)):
            return asyncio.run(create_video(payload, key))

    def _created(self, url, kwargs):
        body = json.loads(kwargs["content"])
        return _response("POST", url, 200, {"ok": True, "idempotent": False, "task": {
            "id": body["taskId"], "status": "queued", "type": body["type"],
        }})

    def test_text_to_video_create_is_signed_and_carries_the_idempotency_key(self):
        model = self._route()
        calls: list = []
        created = self._create(
            {"model": model, "prompt": "雨夜霓虹", "duration": 10},
            {("POST", "/v1/pool/api/tasks"): self._created},
            calls,
            key="caller-key-0001",
        )
        result = json.loads(created.body)
        self.assertEqual(result["status"], "queued")
        (method, url, kwargs), = calls
        self.assertEqual(url, BASE_URL + "/v1/pool/api/tasks")
        assert_signed(self, "POST", url, kwargs, "caller-key-0001")
        self.assertEqual(kwargs["headers"]["X-Idempotency-Key"], "caller-key-0001")
        body = json.loads(kwargs["content"])
        self.assertEqual(body["idempotencyKey"], "caller-key-0001")
        self.assertEqual(body["payload"]["settings"]["duration"], 10)
        self.assertEqual(result["id"], body["taskId"])
        task = database.get_task(body["taskId"])
        self.assertEqual(task["protocol"], wangyue.PROTOCOL)

    def test_reference_images_are_uploaded_as_assets_before_the_task(self):
        model = self._route("seedance2.0mini")
        calls: list = []
        lease_bodies: list = []

        def lease(url, kwargs):
            lease_bodies.append(json.loads(kwargs["content"]))
            return _response("POST", url, 200, {"ok": True, "asset": {"id": f"asset-{len(lease_bodies)}"}, "upload": {
                "method": "PUT", "url": f"https://cos.example/up/{len(lease_bodies)}?sign=x",
                "headers": {"content-type": "image/png", "content-md5": "m"},
            }})

        routes = {
            ("GET", "cdn.example"): lambda url, _kwargs: _response("GET", url, 200, content=PNG),
            ("POST", "/v1/pool/api/assets/input/lease"): lease,
            ("PUT", "cos.example"): lambda url, _kwargs: _response("PUT", url, 200, content=b""),
            ("POST", "/v1/pool/api/assets/asset-1/confirm"): _response(
                "POST", BASE_URL, 200, {"ok": True, "asset": {"id": "asset-1", "state": "ready"}}),
            ("POST", "/v1/pool/api/assets/asset-2/confirm"): _response(
                "POST", BASE_URL, 200, {"ok": True, "asset": {"id": "asset-2", "state": "ready"}}),
            ("POST", "/v1/pool/api/tasks"): self._created,
        }
        created = self._create(
            {"model": model, "prompt": "p", "first_frame_url": "https://cdn.example/f.png",
             "last_frame_url": "https://cdn.example/l.png"},
            routes,
            calls,
        )
        self.assertEqual(json.loads(created.body)["status"], "queued")
        order = [(method, urlsplit(url).netloc if method in {"GET", "PUT"} else urlsplit(url).path)
                 for method, url, _ in calls]
        self.assertEqual(order, [
            ("GET", "cdn.example"), ("POST", "/v1/pool/api/assets/input/lease"), ("PUT", "cos.example"),
            ("POST", "/v1/pool/api/assets/asset-1/confirm"),
            ("GET", "cdn.example"), ("POST", "/v1/pool/api/assets/input/lease"), ("PUT", "cos.example"),
            ("POST", "/v1/pool/api/assets/asset-2/confirm"),
            ("POST", "/v1/pool/api/tasks"),
        ])
        create_kwargs = calls[-1][2]
        body = json.loads(create_kwargs["content"])
        self.assertEqual(body["payload"]["referenceImageAssetIds"], ["asset-1", "asset-2"])
        self.assertEqual(body["payload"]["settings"]["referenceMode"], "start_end")
        self.assertEqual({item["taskId"] for item in lease_bodies}, {body["taskId"]})
        self.assertEqual(lease_bodies[0]["sha256"], hashlib.sha256(PNG).hexdigest())
        self.assertEqual(lease_bodies[0]["sizeBytes"], len(PNG))
        self.assertEqual(lease_bodies[0]["contentType"], "image/png")
        put_kwargs = calls[2][2]
        self.assertEqual(put_kwargs["headers"], {"content-type": "image/png", "content-md5": "m"})
        self.assertNotIn("Authorization", put_kwargs["headers"])
        assert_signed(self, "POST", calls[1][1], calls[1][2])
        assert_signed(self, "POST", calls[3][1], calls[3][2])

    def test_failed_image_upload_fails_without_creating_a_task(self):
        model = self._route("seedance2.0mini")
        calls: list = []
        routes = {
            ("GET", "cdn.example"): lambda url, _kwargs: _response("GET", url, 200, content=b"<html>"),
        }
        with self.assertRaises(HTTPException) as raised:
            self._create({"model": model, "prompt": "p", "image_urls": ["https://cdn.example/a"]}, routes, calls)
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("JPEG", raised.exception.detail)
        self.assertFalse([call for call in calls if call[0] == "POST"])

        calls.clear()
        routes = {
            ("GET", "cdn.example"): lambda url, _kwargs: _response("GET", url, 200, content=PNG),
            ("POST", "/v1/pool/api/assets/input/lease"): lambda url, _kwargs: _response(
                "POST", url, 500, {"ok": False, "error": "internal"}),
        }
        with self.assertRaises(HTTPException) as raised:
            self._create({"model": model, "prompt": "p", "image_urls": ["https://cdn.example/a"]}, routes, calls)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertFalse([call for call in calls if urlsplit(call[1]).path == "/v1/pool/api/tasks"])

    def test_private_image_links_are_refused(self):
        model = self._route("seedance2.0mini")

        async def private_dns(host, port, **_kwargs):
            return [(2, 1, 6, "", ("10.0.0.5", port))]

        with patch("app.media_probe._resolve", private_dns), self.assertRaises(HTTPException) as raised:
            self._create({"model": model, "prompt": "p", "image_urls": ["https://intranet.example/a.png"]}, {}, [])
        self.assertEqual(raised.exception.status_code, 400)

    def test_bad_key_fails_before_anything_is_sent(self):
        model = self._route(api_key="no-colon")
        calls: list = []
        with self.assertRaises(HTTPException) as raised:
            self._create({"model": model, "prompt": "p", "duration": 10}, {}, calls)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(calls, [])

    def test_route_durations_are_enforced(self):
        model = self._route("seedance2.0mini")
        with self.assertRaises(HTTPException) as raised:
            self._create({"model": model, "prompt": "p", "duration": 10}, {}, [])
        self.assertEqual(raised.exception.status_code, 400)

    def test_poll_statuses_and_download(self):
        model = self._route()
        calls: list = []
        created = self._create(
            {"model": model, "prompt": "p", "duration": 15},
            {("POST", "/v1/pool/api/tasks"): self._created},
            calls,
        )
        task_id = json.loads(created.body)["id"]
        poll_path = f"/v1/pool/api/tasks/{task_id}"

        def poll(status: str, http_status: int = 200, body=None):
            calls.clear()
            routes = {("GET", poll_path): _response("GET", BASE_URL, http_status, body or task_answer(task_id, status))}
            with patch("app.proxy.httpx.AsyncClient", fake_client(calls, routes)):
                result = json.loads(asyncio.run(fetch_task(task_id)).body)
            assert_signed(self, "GET", calls[0][1], calls[0][2])
            return result

        self.assertEqual(poll("leased")["status"], "processing")
        self.assertEqual(poll("review_required")["status"], "processing")
        # A rejected signature keeps the task running instead of failing it.
        rejected = poll("", 401, {"ok": False, "error": "pool_api_unauthorized", "message": "bad"})
        self.assertEqual(rejected["status"], "processing")
        self.assertEqual(database.get_task(task_id)["status"], "processing")
        # The live gateway answers an unknown key ID with HTTP 400.
        disabled_key = poll("", 400, {"ok": False, "error": "api_key_id_invalid", "message": "invalid"})
        self.assertEqual(disabled_key["status"], "processing")
        self.assertEqual(poll("completed")["status"], "completed")

        downloaded: dict = {}

        async def mock_stream(source_url, _request, headers=None, **kwargs):
            downloaded.update(url=source_url, headers=headers)
            return Response(content=b"video", media_type="video/mp4")

        calls.clear()
        result_routes = {("GET", f"{poll_path}/result"): _response("GET", BASE_URL, 200, {
            "ok": True,
            "asset": {"id": "r1", "state": "ready"},
            "download": {"method": "GET", "url": "https://cos.example/result.mp4?sign=1", "headers": {}},
        })}
        request = httpx.Request("GET", f"https://relay.example/v1/videos/{task_id}/content")
        with patch("app.proxy.httpx.AsyncClient", fake_client(calls, result_routes)), patch(
            "app.proxy.stream_upstream_content", new=mock_stream
        ):
            asyncio.run(stream_content(task_id, request))
        self.assertEqual(downloaded["url"], "https://cos.example/result.mp4?sign=1")
        self.assertEqual(downloaded["headers"], {})
        assert_signed(self, "GET", calls[0][1], calls[0][2])

    def test_failed_and_cancelled_tasks_report_failure(self):
        model = self._route()
        created = self._create(
            {"model": model, "prompt": "p", "duration": 10},
            {("POST", "/v1/pool/api/tasks"): self._created},
            [],
        )
        task_id = json.loads(created.body)["id"]
        routes = {("GET", f"/v1/pool/api/tasks/{task_id}"): _response(
            "GET", BASE_URL, 200, task_answer(task_id, "failed", error={"message": "号池节点执行失败"}))}
        with patch("app.proxy.httpx.AsyncClient", fake_client([], routes)):
            result = json.loads(asyncio.run(fetch_task(task_id)).body)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["message"], "号池节点执行失败")

    def test_unconfirmed_create_is_bound_by_reading_the_task(self):
        model = self._route()
        calls: list = []
        created = self._create(
            {"model": model, "prompt": "p", "duration": 10},
            {("POST", "/v1/pool/api/tasks"): httpx.ReadTimeout("lost")},
            calls,
        )
        provisional = json.loads(created.body)
        self.assertTrue(provisional["id"].startswith("vrq_"))
        self.assertEqual(provisional["status"], "queued")
        self.assertEqual(len([call for call in calls if call[0] == "POST"]), 1)
        task_id = json.loads(calls[0][2]["content"])["taskId"]

        calls.clear()
        routes = {("GET", f"/v1/pool/api/tasks/{task_id}"): lambda url, _kwargs: _response(
            "GET", url, 200, task_answer(task_id, "leased"))}
        with patch("app.proxy.httpx.AsyncClient", fake_client(calls, routes)):
            result = json.loads(asyncio.run(fetch_task(provisional["id"])).body)
        self.assertEqual(result["status"], "processing")
        self.assertTrue(all(method == "GET" for method, _url, _kwargs in calls))
        self.assertEqual(database.get_create_submission(provisional["id"])["upstream_task_id"], task_id)

    def test_unconfirmed_create_stays_waiting_when_the_pool_does_not_know_it(self):
        model = self._route()
        created = self._create(
            {"model": model, "prompt": "p", "duration": 10},
            {("POST", "/v1/pool/api/tasks"): httpx.ReadTimeout("lost")},
            [],
        )
        provisional = json.loads(created.body)["id"]
        routes = {("GET", BASE_URL.split("//", 1)[1]): lambda url, _kwargs: _response(
            "GET", url, 404, {"ok": False, "error": "pool_task_not_found"})}
        with patch("app.proxy.httpx.AsyncClient", fake_client([], routes)):
            result = json.loads(asyncio.run(fetch_task(provisional)).body)
        self.assertEqual(result["status"], "queued")
        self.assertIsNone(database.get_create_submission(provisional)["upstream_task_id"])

    def test_definite_rejection_fails_the_create(self):
        model = self._route()
        created = self._create(
            {"model": model, "prompt": "p", "duration": 10},
            {("POST", "/v1/pool/api/tasks"): _response(
                "POST", BASE_URL, 429, {"ok": False, "error": "pool_api_concurrency_limit", "message": "busy"})},
            [],
        )
        self.assertEqual(created.status_code, 429)
        self.assertEqual(json.loads(created.body)["error"]["message"], "busy")


class WangyueDiscoveryTests(unittest.TestCase):
    def test_sync_reads_the_public_catalog_without_sending_the_key(self):
        database.initialize()
        calls: list = []
        routes = {("GET", wangyue.MODELS_PATH): _response("GET", BASE_URL, 200, {"ok": True, "models": [
            {"modelId": "seedance2.0fast", "metadata": {"capability": "video"}},
            {"modelId": "seedance2.0mini", "metadata": {"capability": "video"}},
            {"modelId": "vidu-q3", "metadata": {"capability": "video"}},
        ]})}
        client = TestClient(app)
        session = create_session("admin")
        client.cookies.set(SESSION_COOKIE, session)
        with patch("app.main.httpx.AsyncClient", fake_client(calls, routes)):
            response = client.post(
                "/admin/api/upstreams/models",
                headers={"X-CSRF-Token": csrf_token(session)},
                json={"base_url": BASE_URL, "api_key": API_KEY},
            )
        self.assertEqual(response.status_code, 200, response.text)
        (method, url, kwargs), = calls
        self.assertEqual(url, BASE_URL + wangyue.MODELS_PATH)
        self.assertNotIn("Authorization", kwargs["headers"])
        models = {item["upstream_model"]: item for item in response.json()["models"]}
        self.assertEqual(models["seedance2.0fast"]["profile"], "wangyue-seedance-fast")
        self.assertEqual(models["seedance2.0mini"]["durations"], [15])
        self.assertEqual(models["vidu-q3"]["protocol"], wangyue.PROTOCOL)
        self.assertEqual(models["seedance2.0fast"]["video_count"], 0)


if __name__ == "__main__":
    unittest.main()
