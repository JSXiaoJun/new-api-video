from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

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

from app.channels import funai
from app.main import normalize_discovered_models
from app.proxy import create_video, fetch_task, reconcile_pending_tasks


class FunAIChannelTests(unittest.TestCase):
    def test_model_discovery_filters_non_video_models_and_assigns_profiles(self):
        models = normalize_discovered_models(
            {
                "data": [
                    {"id": "gpt-image-2"},
                    {"id": "minimax-h3"},
                    {"id": "veo-3.1-fast"},
                    {"id": "kling-v3"},
                    {"id": "kling-v3-omni-v2v-create"},
                    {"id": "nano-banana2"},
                ]
            },
            funai.PROTOCOL,
        )

        self.assertEqual(
            [item["upstream_model"] for item in models],
            ["minimax-h3", "veo-3.1-fast", "kling-v3", "kling-v3-omni-v2v-create"],
        )
        self.assertTrue(all(item["protocol"] == funai.PROTOCOL for item in models))
        self.assertEqual(models[0]["profile"], "funai-minimax-h3")
        self.assertEqual(models[1]["profile"], "funai-veo")
        self.assertEqual(models[1]["durations"], [8])
        self.assertEqual(models[2]["profile"], "funai-kling-frames")
        self.assertEqual(models[3]["profile"], "funai-kling")

    def test_payload_mapping_is_model_specific(self):
        minimax = funai.transform_create_payload({
            "model": "minimax-h3",
            "prompt": "Night market",
            "duration": 8,
            "aspect_ratio": "16:9",
            "resolution": "1440p",
            "generate_audio": True,
            "image_urls": ["https://cdn.example/subject.png"],
            "audio_urls": ["https://cdn.example/ambient.mp3"],
            "metadata": {"private": "ignored"},
        })
        self.assertEqual(minimax, {
            "model": "minimax-h3",
            "prompt": "Night market",
            "seconds": 8,
            "aspect_ratio": "16:9",
            "resolution": "1440p",
            "audio": True,
            "reference_images": ["https://cdn.example/subject.png"],
            "audio_reference": ["https://cdn.example/ambient.mp3"],
        })

        kling = funai.transform_create_payload({
            "model": "kling-o3",
            "prompt": "Restyle the clip",
            "duration": 10,
            "image_urls": ["https://cdn.example/identity.png"],
            "reference_video": "https://cdn.example/source.mp4",
        })
        self.assertEqual(kling["reference_images"], ["https://cdn.example/identity.png"])
        self.assertEqual(kling["input_video"], "https://cdn.example/source.mp4")
        self.assertNotIn("seconds", kling)

        kling_reference = funai.transform_create_payload({
            "model": "kling-o3-pro-v2v-reference",
            "prompt": "Keep both characters",
            "seconds": 10,
            "image_urls": [
                "https://cdn.example/character-one.png",
                "https://cdn.example/character-two.png",
            ],
        })
        self.assertEqual(kling_reference["element_references"], [
            "https://cdn.example/character-one.png",
            "https://cdn.example/character-two.png",
        ])
        self.assertNotIn("images", kling_reference)

        explicit_frames = funai.transform_create_payload({
            "model": "kling-o3-pro-v2v-reference",
            "prompt": "Use both character references",
            "images": [
                "https://cdn.example/character-one.png",
                "https://cdn.example/character-two.png",
            ],
        })
        self.assertEqual(explicit_frames["element_references"], [
            "https://cdn.example/character-one.png",
            "https://cdn.example/character-two.png",
        ])
        self.assertNotIn("images", explicit_frames)

        kling_v3 = funai.transform_create_payload({
            "model": "kling-v3",
            "prompt": "Animate the two characters",
            "image_urls": ["https://cdn.example/character.png"],
        })
        self.assertEqual(kling_v3["element_references"], ["https://cdn.example/character.png"])
        self.assertNotIn("images", kling_v3)

        kling_omni = funai.transform_create_payload({
            "model": "kling-v3-omni-v2v-create",
            "prompt": "Restyle the characters",
            "image_urls": ["https://cdn.example/character.png"],
        })
        self.assertEqual(kling_omni["element_references"], ["https://cdn.example/character.png"])
        self.assertNotIn("images", kling_omni)

        kling_o3_frames = funai.transform_create_payload({
            "model": "kling-o3",
            "prompt": "Animate between two frames",
            "image_urls": [
                "https://cdn.example/start.png",
                "https://cdn.example/end.png",
                "https://cdn.example/ignored.png",
            ],
        }, "funai-kling-frames")
        self.assertEqual(kling_o3_frames["start_frame"], "https://cdn.example/start.png")
        self.assertEqual(kling_o3_frames["end_frame"], "https://cdn.example/end.png")
        self.assertNotIn("reference_images", kling_o3_frames)
        self.assertNotIn("element_references", kling_o3_frames)

        kling_v2v_frames = funai.transform_create_payload({
            "model": "kling-o3-pro-v2v-reference",
            "prompt": "Animate between two frames",
            "image_urls": [
                "https://cdn.example/start.png",
                "https://cdn.example/end.png",
                "https://cdn.example/ignored.png",
            ],
        }, "funai-kling-frames")
        self.assertEqual(kling_v2v_frames["images"], [
            "https://cdn.example/start.png",
            "https://cdn.example/end.png",
        ])
        self.assertNotIn("element_references", kling_v2v_frames)

        kling_omni_frames = funai.transform_create_payload({
            "model": "kling-v3-omni-v2v-create",
            "prompt": "Keep the character",
            "image_urls": ["https://cdn.example/character.png"],
        }, "funai-kling-frames")
        self.assertEqual(
            kling_omni_frames["element_references"],
            ["https://cdn.example/character.png"],
        )
        self.assertNotIn("images", kling_omni_frames)
        self.assertNotIn("start_frame", kling_omni_frames)

        veo = funai.transform_create_payload({
            "model": "veo-3.1",
            "prompt": "Product shot",
            "seconds": 6,
            "images": ["https://cdn.example/product.png"],
            "size": "1920x1080",
        })
        self.assertEqual(veo["images"], ["https://cdn.example/product.png"])
        self.assertEqual(veo["size"], "1920x1080")

        unsupported_video = funai.transform_create_payload({
            "model": "veo-3.1",
            "prompt": "Ignore unsupported source video",
            "reference_video": "https://cdn.example/source.mp4",
        })
        self.assertNotIn("input_video", unsupported_video)

    def test_omni_maps_local_reference_image_aliases(self):
        singular = funai.transform_create_payload({
            "model": "gemini-omni",
            "prompt": "Keep the product identity",
            "reference_image": "https://cdn.example/product.png",
        })
        self.assertEqual(singular["image_reference"], "https://cdn.example/product.png")
        self.assertNotIn("reference_image", singular)
        self.assertNotIn("reference_images", singular)

        plural = funai.transform_create_payload({
            "model": "gemini-omni-flash",
            "prompt": "Use all references",
            "reference_images": [
                "https://cdn.example/product.png",
                "https://cdn.example/style.png",
            ],
        })
        self.assertEqual(plural["reference_images"], [
            "https://cdn.example/product.png",
            "https://cdn.example/style.png",
        ])
        self.assertNotIn("image_reference", plural)

    def test_omni_does_not_emit_duplicate_reference_fields(self):
        payload = funai.transform_create_payload({
            "model": "gemini-omni",
            "prompt": "Prefer the explicit local alias",
            "reference_image": "https://cdn.example/explicit.png",
            "image_urls": ["https://cdn.example/legacy.png"],
        })

        self.assertEqual(payload["image_reference"], "https://cdn.example/explicit.png")
        self.assertNotIn("reference_image", payload)
        self.assertNotIn("reference_images", payload)

    def test_create_and_poll_use_funai_adapter_only(self):
        captured: dict[str, object] = {}
        task_id = "video_funai_1"
        task = {
            "task_id": task_id,
            "api_key": "funai-secret",
            "base_url": "https://api.funai.works/v1",
            "protocol": funai.PROTOCOL,
            "model": "public-kling",
            "created_at": 100,
            "relay_request_id": "vrq_funai",
            "public_task_id": None,
        }

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, url, **kwargs):
                captured["post"] = (url, kwargs)
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    json={"id": task_id, "status": "queued", "progress": 0},
                )

            async def get(self, url, **kwargs):
                captured["get"] = (url, kwargs)
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", url),
                    json={
                        "id": task_id,
                        "status": "completed",
                        "progress": 100,
                        "url": "https://api.funai.works/generated/result.mp4",
                        "content_url": f"https://api.funai.works/v1/videos/{task_id}/content",
                    },
                )

        upstream = {
            "id": 41,
            "api_key": "funai-secret",
            "base_url": "https://api.funai.works/v1",
            "protocol": funai.PROTOCOL,
            "profile": "funai-kling-frames",
            "upstream_model": "kling-v3",
            "forward_resolution": True,
        }
        with (
            patch("app.proxy.database.select_upstream", return_value=upstream),
            patch("app.proxy.database.start_audit_request", return_value="vrq_funai"),
            patch("app.proxy.database.record_upstream_request_payload"),
            patch("app.proxy.database.record_audit_event"),
            patch("app.proxy.database.create_task"),
            patch("app.proxy.database.get_task", return_value=task),
            patch("app.proxy.database.touch_task"),
            patch("app.proxy.database.update_task") as update_task,
            patch("app.proxy.httpx.AsyncClient", MockAsyncClient),
        ):
            created = asyncio.run(create_video({
                "model": "public-kling",
                "prompt": "Animate between the frames",
                "duration": 8,
                "aspect_ratio": "16:9",
                "resolution": "1080p",
                "image_urls": [
                    "https://cdn.example/start.png",
                    "https://cdn.example/end.png",
                ],
            }, None))
            fetched = asyncio.run(fetch_task(task_id))

        post_url, post_options = captured["post"]
        self.assertEqual(post_url, "https://api.funai.works/v1/videos")
        self.assertEqual(post_options["json"], {
            "model": "kling-v3",
            "prompt": "Animate between the frames",
            "seconds": 8,
            "aspect_ratio": "16:9",
            "resolution": "1080p",
            "start_frame": "https://cdn.example/start.png",
            "end_frame": "https://cdn.example/end.png",
        })
        self.assertEqual(captured["get"][0], f"https://api.funai.works/v1/videos/{task_id}")
        self.assertEqual(json.loads(created.body)["id"], task_id)
        self.assertEqual(json.loads(fetched.body)["status"], "completed")
        update_task.assert_called_once_with(
            task_id,
            "completed",
            "https://api.funai.works/generated/result.mp4",
            None,
        )

    def test_failed_poll_updates_local_task_and_audit_status(self):
        task_id = "video_funai_failed"
        task = {
            "task_id": task_id,
            "api_key": "funai-secret",
            "base_url": "https://api.funai.works/v1",
            "protocol": funai.PROTOCOL,
            "model": "public-kling",
            "created_at": 100,
            "relay_request_id": "vrq_funai_failed",
            "public_task_id": None,
        }
        response = httpx.Response(
            200,
            request=httpx.Request("GET", f"https://api.funai.works/v1/videos/{task_id}"),
            json={
                "id": task_id,
                "error": {"message": "upstream returned an invalid response"},
            },
        )

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, *_args, **_kwargs):
                return response

        with (
            patch("app.proxy.database.get_task", return_value=task),
            patch("app.proxy.database.touch_task"),
            patch("app.proxy.database.update_task") as update_task,
            patch("app.proxy.database.record_audit_event"),
            patch("app.proxy.httpx.AsyncClient", MockAsyncClient),
        ):
            result = asyncio.run(fetch_task(task_id))

        self.assertEqual(json.loads(result.body)["status"], "failed")
        update_task.assert_called_once_with(
            task_id,
            "failed",
            None,
            "upstream returned an invalid response",
        )

    def test_non_retryable_poll_http_error_marks_task_failed(self):
        task_id = "video_funai_missing"
        task = {
            "task_id": task_id,
            "api_key": "funai-secret",
            "base_url": "https://api.funai.works/v1",
            "protocol": funai.PROTOCOL,
            "model": "public-kling",
            "created_at": 100,
            "relay_request_id": "vrq_funai_missing",
            "public_task_id": None,
        }
        response = httpx.Response(
            404,
            request=httpx.Request("GET", f"https://api.funai.works/v1/videos/{task_id}"),
            json={"error": {"message": "Task expired or does not exist"}},
        )

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, *_args, **_kwargs):
                return response

        with (
            patch("app.proxy.database.get_task", return_value=task),
            patch("app.proxy.database.touch_task"),
            patch("app.proxy.database.update_task") as update_task,
            patch("app.proxy.database.record_audit_event"),
            patch("app.proxy.httpx.AsyncClient", MockAsyncClient),
        ):
            result = asyncio.run(fetch_task(task_id))

        self.assertEqual(result.status_code, 404)
        update_task.assert_called_once_with(task_id, "failed", None, "Task expired or does not exist")

    def test_cloudflare_520_poll_error_is_retryable_and_does_not_fail_task(self):
        """A transient Cloudflare origin error must not end an active job."""
        task_id = "video_funai_cloudflare_520"
        task = {
            "task_id": task_id,
            "api_key": "funai-secret",
            "base_url": "https://api.funai.works/v1",
            "protocol": funai.PROTOCOL,
            "model": "public-kling",
            "created_at": 100,
            "relay_request_id": "vrq_funai_cloudflare_520",
            "public_task_id": None,
        }
        response = httpx.Response(
            520,
            request=httpx.Request("GET", f"https://api.funai.works/v1/videos/{task_id}"),
            json={
                "status": 520,
                "error_name": "unknown_origin_error",
                "retryable": True,
                "retry_after": 60,
            },
        )

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, *_args, **_kwargs):
                return response

        with (
            patch("app.proxy.database.get_task", return_value=task),
            patch("app.proxy.database.touch_task"),
            patch("app.proxy.database.update_task") as update_task,
            patch("app.proxy.database.record_audit_event"),
            patch("app.proxy.httpx.AsyncClient", MockAsyncClient),
        ):
            result = asyncio.run(fetch_task(task_id))

        self.assertEqual(result.status_code, 520)
        self.assertEqual(result.headers.get("retry-after"), "60")
        update_task.assert_not_called()

    def test_cloudflare_525_poll_error_does_not_fail_completed_upstream_task(self):
        """A Cloudflare TLS error cannot prove that the provider job failed."""
        task_id = "video_funai_cloudflare_525"
        task = {
            "task_id": task_id,
            "api_key": "funai-secret",
            "base_url": "https://api.funai.works/v1",
            "protocol": funai.PROTOCOL,
            "model": "public-kling",
            "created_at": 100,
            "relay_request_id": "vrq_funai_cloudflare_525",
            "public_task_id": None,
        }
        response = httpx.Response(
            525,
            request=httpx.Request("GET", f"https://api.funai.works/v1/videos/{task_id}"),
            json={"status": 525, "error_name": "ssl_handshake_failed", "retryable": False},
        )

        class MockAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, *_args, **_kwargs):
                return response

        with (
            patch("app.proxy.database.get_task", return_value=task),
            patch("app.proxy.database.touch_task"),
            patch("app.proxy.database.update_task") as update_task,
            patch("app.proxy.database.record_audit_event"),
            patch("app.proxy.httpx.AsyncClient", MockAsyncClient),
        ):
            result = asyncio.run(fetch_task(task_id))

        self.assertEqual(result.status_code, 525)
        update_task.assert_not_called()

    def test_reconciler_refreshes_all_stale_pending_tasks(self):
        fetch = AsyncMock()
        with (
            patch("app.proxy.database.list_pending_task_ids", return_value=["task_one", "task_two"]),
            patch("app.proxy.fetch_task", fetch),
        ):
            asyncio.run(reconcile_pending_tasks(limit=2, stale_seconds=5))

        self.assertEqual({call.args[0] for call in fetch.await_args_list}, {"task_one", "task_two"})
        self.assertTrue(all(call.kwargs["timeout_seconds"] <= 5 for call in fetch.await_args_list))


if __name__ == "__main__":
    unittest.main()
