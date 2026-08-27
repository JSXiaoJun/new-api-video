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

from app import database
from app.channels import autodl_comfyui
from app.main import normalize_discovered_models
from app.proxy import create_video, fetch_task


class AutoDLComfyUIAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()

    def test_known_models_get_isolated_protocol_and_capabilities(self):
        routes = normalize_discovered_models(
            list(autodl_comfyui.KNOWN_MODELS), autodl_comfyui.PROTOCOL
        )
        by_model = {route["upstream_model"]: route for route in routes}

        text = by_model["minimax_h3_lightx2v_no_pic"]
        self.assertEqual(text["protocol"], autodl_comfyui.PROTOCOL)
        self.assertEqual(text["profile"], autodl_comfyui.PROFILE)
        self.assertEqual(text["durations"], list(range(1, 16)))
        self.assertEqual(text["resolutions"], ["480p", "768p"])
        self.assertEqual(text["image_count"], 0)
        self.assertFalse(text["supports_audio"])

        multimodal = by_model["minimax_h3_image_audio_to_video_v2"]
        self.assertEqual(multimodal["image_count"], 9)
        self.assertTrue(multimodal["supports_audio"])
        self.assertEqual(multimodal["resolutions"], ["480p", "768p", "1080p"])

    def test_text_payload_combines_resolution_and_aspect_ratio(self):
        payload = autodl_comfyui.transform_create_payload({
            "model": "minimax_h3_lightx2v_no_pic",
            "prompt": "海边日落",
            "duration": 5,
            "resolution": "480p",
            "aspect_ratio": "16:9",
        })

        self.assertEqual(payload, {
            "prompt": "海边日落",
            "duration": 5,
            "resolution": "480p横",
        })

    def test_multimodal_payload_maps_reference_arrays(self):
        payload = autodl_comfyui.transform_create_payload({
            "model": "minimax_h3_image_audio_to_video_v2_15s",
            "prompt": "角色说话",
            "seconds": 8,
            "resolution": "768p",
            "aspect_ratio": "9:16",
            "image_urls": ["https://cdn.example/one.png", "https://cdn.example/two.png"],
            "audio_urls": ["https://cdn.example/voice.wav"],
            "seed": 123,
        })

        self.assertEqual(payload, {
            "prompt": "角色说话",
            "duration": 8,
            "resolution": "768p竖",
            "ref_image_0": "https://cdn.example/one.png",
            "ref_image_1": "https://cdn.example/two.png",
            "ref_audio_0": "https://cdn.example/voice.wav",
            "seed": 123,
        })

    def test_first_last_frame_and_promptless_workflows(self):
        first_last = autodl_comfyui.transform_create_payload({
            "model": "minimax_h3_lightx2v",
            "prompt": "镜头向前移动",
            "duration": 6,
            "first_frame": {"url": "https://cdn.example/first.png"},
            "last_frame_url": "https://cdn.example/last.png",
        })
        motion = autodl_comfyui.transform_create_payload({
            "model": "wan2.2animate-v4-motion_retargeting",
            "image_url": "https://cdn.example/person.png",
            "reference_video": "https://cdn.example/dance.mp4",
        })

        self.assertEqual(first_last["first_frame"], "https://cdn.example/first.png")
        self.assertEqual(first_last["last_frame"], "https://cdn.example/last.png")
        self.assertEqual(motion, {
            "ref_image": "https://cdn.example/person.png",
            "ref_video": "https://cdn.example/dance.mp4",
        })
        self.assertFalse(autodl_comfyui.requires_prompt("wan2.2animate-v4-motion_retargeting"))
        self.assertFalse(autodl_comfyui.requires_prompt("minimax_h3_image_audio_to_video"))

    def test_lip_sync_renames_duration_and_primary_image_precedes_references(self):
        lip_sync = autodl_comfyui.transform_create_payload({
            "model": "minimax_h3_image_audio_to_video",
            "duration": 7,
            "resolution": "768p横",
            "image_url": "https://cdn.example/face.png",
            "audio_url": "https://cdn.example/voice.wav",
        })
        multi_image = autodl_comfyui.transform_create_payload({
            "model": "minimax_h3_lightx2v_v5",
            "prompt": "保持人物一致",
            "image_url": "https://cdn.example/main.png",
            "reference_image_urls": ["https://cdn.example/reference.png"],
        })

        self.assertEqual(lip_sync, {
            "audio_duration": 7,
            "resolution": "768p横",
            "ref_image_0": "https://cdn.example/face.png",
            "ref_audio_0": "https://cdn.example/voice.wav",
        })
        self.assertNotIn("duration", lip_sync)
        self.assertEqual(multi_image["ref_image_0"], "https://cdn.example/main.png")
        self.assertEqual(multi_image["ref_image_1"], "https://cdn.example/reference.png")

    def test_nested_create_and_task_responses_are_parsed(self):
        create_payload = {
            "code": "Success",
            "data": {"task_id": "task/1", "status": "QUEUED"},
        }
        task_payload = {
            "code": "Success",
            "data": {
                "status": "SUCCESS",
                "results": [{
                    "url": "https://cdn.example/result.mp4",
                    "type": "video",
                    "file_type": "mp4",
                }],
            },
        }

        self.assertEqual(autodl_comfyui.extract_create_task_id(create_payload), "task/1")
        self.assertEqual(autodl_comfyui.extract_create_status(create_payload), "QUEUED")
        fields = autodl_comfyui.extract_task_fields(task_payload)
        self.assertEqual(fields["status"], "SUCCESS")
        self.assertEqual(fields["video_url"], "https://cdn.example/result.mp4")
        self.assertEqual(autodl_comfyui.task_path("task/1"), (
            "/api/v1/comfyui/comfyui_workflow/result/task%2F1"
        ))

    def test_proxy_create_and_poll_use_autodl_paths_and_raw_token(self):
        public_model = f"autodl-public-{time.time_ns()}"
        workflow_id = "minimax_h3_lightx2v_no_pic"
        database.save_upstream({
            "name": public_model,
            "base_url": "https://autodl.art",
            "api_key": "autodl-secret",
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": public_model,
                "upstream_model": workflow_id,
                "protocol": autodl_comfyui.PROTOCOL,
                "profile": autodl_comfyui.PROFILE,
                "durations": [1, 15],
                "resolutions": ["480p", "768p"],
                "image_count": 0,
                "supports_image": False,
                "supports_video": False,
                "supports_audio": False,
            }],
        })
        captured: dict[str, tuple[str, dict]] = {}
        task_id = f"autodl-task-{time.time_ns()}"

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
                    "code": "Success",
                    "data": {"task_id": task_id, "status": "QUEUED"},
                })

            async def get(self, url, **kwargs):
                captured["get"] = (url, kwargs)
                return httpx.Response(200, request=httpx.Request("GET", url), json={
                    "code": "Success",
                    "data": {
                        "status": "SUCCESS",
                        "results": [{"url": "https://cdn.example/video.mp4", "type": "video"}],
                    },
                })

        with patch("app.proxy.httpx.AsyncClient", MockAsyncClient):
            created = asyncio.run(create_video({
                "model": public_model,
                "prompt": "纸飞机穿过云层",
                "duration": 1,
                "resolution": "480p",
                "aspect_ratio": "9:16",
            }, None))
            fetched = asyncio.run(fetch_task(task_id))

        self.assertEqual(created.status_code, 200)
        self.assertEqual(json.loads(created.body)["status"], "queued")
        self.assertEqual(captured["post"][0], (
            f"https://autodl.art/api/v1/comfyui/comfyui_workflow/{workflow_id}"
        ))
        self.assertEqual(captured["post"][1]["headers"]["Authorization"], "autodl-secret")
        self.assertEqual(captured["post"][1]["json"], {
            "prompt": "纸飞机穿过云层",
            "duration": 1,
            "resolution": "480p竖",
        })
        self.assertEqual(captured["get"][0], (
            f"https://autodl.art/api/v1/comfyui/comfyui_workflow/result/{task_id}"
        ))
        self.assertEqual(captured["get"][1]["headers"]["Authorization"], "autodl-secret")
        fetched_payload = json.loads(fetched.body)
        self.assertEqual(fetched_payload["status"], "completed")
        task = database.get_task(task_id)
        self.assertEqual(task["source_video_url"], "https://cdn.example/video.mp4")


if __name__ == "__main__":
    unittest.main()
