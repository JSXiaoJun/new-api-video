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
from app.model_profiles import capabilities_for


ZM_WORKFLOWS = ("minimax_h3_zm_u24", "minimax_h3_zm_u08")


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
            "duration": 6,
            "resolution": "768p",
            "aspect_ratio": "3:4",
            "generate_audio": True,
            "image_urls": ["https://cdn.example/ignored.png"],
            "seed": 123,
        })

        self.assertEqual(payload, {
            "prompt": "海边日落",
            "duration": 6,
            "resolution": "768p竖",
        })

    def test_zm_models_are_discovered_with_workbench_capabilities(self):
        routes = normalize_discovered_models(
            list(autodl_comfyui.KNOWN_MODELS), autodl_comfyui.PROTOCOL
        )
        by_model = {route["upstream_model"]: route for route in routes}
        for model in ZM_WORKFLOWS:
            with self.subTest(model=model):
                route = by_model[model]
                self.assertEqual(route["protocol"], autodl_comfyui.PROTOCOL)
                self.assertEqual(route["profile"], autodl_comfyui.PROFILE)
                self.assertTrue(route["supports_image"])
                self.assertTrue(route["supports_audio"])
                self.assertFalse(route["supports_video"])
                # 数量是执行依据：勾选换算成明确的数字，0 = 不支持。
                self.assertEqual(route["image_count"], 9)
                self.assertEqual(route["video_count"], 0)
                self.assertEqual(route["audio_count"], 3)
                self.assertTrue(autodl_comfyui.requires_prompt(model))
                caps = capabilities_for(
                    route["profile"], route["durations"], route["image_count"],
                    route["video_count"], route["audio_count"], route["resolutions"],
                )
                self.assertEqual(caps["durations"], list(range(1, 16)))
                self.assertEqual(caps["resolutions"], ["480p", "768p"])
                self.assertEqual(caps["ratios"], ["16:9", "9:16", "1:1"])
                self.assertEqual(caps["maxImages"], 9)
                self.assertEqual(caps["maxAudios"], 3)
                self.assertFalse(caps["referenceVideo"])

    def test_zm_reference_arrays_and_resolution_variants(self):
        images = [f"https://cdn.example/{index}.png" for index in range(10)]
        audios = [f"https://cdn.example/{index}.wav" for index in range(4)]
        for model in ZM_WORKFLOWS:
            for resolution in ("480p", "768p"):
                for ratio, suffix in (("16:9", "横"), ("9:16", "竖"), ("1:1", "(1:1)")):
                    with self.subTest(model=model, resolution=resolution, ratio=ratio):
                        result = autodl_comfyui.transform_create_payload({
                            "model": model, "prompt": " test ", "seconds": 15,
                            "resolution": resolution, "aspect_ratio": ratio,
                            "image_urls": images, "audio_urls": audios, "seed": 0,
                            "video_url": "https://cdn.example/ignored.mp4",
                        })
                        self.assertEqual(result, {
                            "prompt": "test", "duration": 15,
                            "resolution": resolution + suffix, "seed": 0,
                            **{f"ref_image_{i}": url for i, url in enumerate(images[:9])},
                            **{f"ref_audio_{i}": url for i, url in enumerate(audios[:3])},
                        })

    def test_zm_native_fields_and_optional_defaults(self):
        for model in ZM_WORKFLOWS:
            with self.subTest(model=model):
                native = {
                    "prompt": "test", "duration": 1, "resolution": "768p横",
                    "seed": 999999999999999,
                    **{f"ref_image_{i}": f"https://cdn.example/{i}.png" for i in range(9)},
                    **{f"ref_audio_{i}": f"https://cdn.example/{i}.flac" for i in range(3)},
                }
                self.assertEqual(autodl_comfyui.transform_create_payload({
                    "model": model, **native,
                    "ref_audio_0": {"audio_url": {"url": native["ref_audio_0"]}},
                    "ref_image_0": {"url": native["ref_image_0"]},
                    "image_urls": ["https://cdn.example/ignored.png"],
                    "audio_urls": ["https://cdn.example/ignored.wav"],
                    "ref_image_9": "https://cdn.example/extra.png",
                    "ref_audio_3": "https://cdn.example/extra.wav",
                }), native)
                self.assertEqual(autodl_comfyui.transform_create_payload({
                    "model": model, "prompt": "test",
                    "image_url": "https://cdn.example/image.png",
                }), {"prompt": "test", "ref_image_0": "https://cdn.example/image.png"})

    def test_unknown_workflow_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported AutoDL ComfyUI workflow"):
            autodl_comfyui.transform_create_payload({"model": "unknown-workflow", "prompt": "test"})

    def test_multimodal_payload_maps_reference_arrays(self):
        payload = autodl_comfyui.transform_create_payload({
            "model": "minimax_h3_image_audio_to_video_v2_15s",
            "prompt": "角色说话",
            "duration": 15,
            "resolution": "768p",
            "aspect_ratio": "4:3",
            "image_urls": ["https://cdn.example/one.png", "https://cdn.example/two.png"],
            "audio_urls": ["https://cdn.example/voice.wav"],
            "seed": 123,
        })

        self.assertEqual(payload, {
            "prompt": "角色说话",
            "duration": 15,
            "resolution": "768p横",
            "ref_image_0": "https://cdn.example/one.png",
            "ref_image_1": "https://cdn.example/two.png",
            "ref_audio_0": "https://cdn.example/voice.wav",
            "seed": 123,
        })

    def test_resolution_orientation_uses_ratio_dimensions(self):
        cases = {
            "21:9": "768p横",
            "9/16": "768p竖",
            "1x1": "768p(1:1)",
            "4×3": "768p横",
        }
        for aspect_ratio, expected in cases.items():
            with self.subTest(aspect_ratio=aspect_ratio):
                payload = autodl_comfyui.transform_create_payload({
                    "model": "minimax_h3_lightx2v_no_pic",
                    "prompt": "测试画面方向",
                    "resolution": "768p",
                    "aspect_ratio": aspect_ratio,
                })
                self.assertEqual(payload["resolution"], expected)

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
            "prompt": "通用客户端可能多传的提示词",
            "duration": 6,
            "resolution": "768p",
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
            "prompt": "通用客户端可能多传的提示词",
            "duration": 7,
            "resolution": "768p横",
            "seed": 123,
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
        self._assert_proxy_create_and_poll("minimax_h3_lightx2v_no_pic")

    def test_zm_proxy_create_and_poll(self):
        for model in ZM_WORKFLOWS:
            with self.subTest(model=model):
                self._assert_proxy_create_and_poll(model)

    def _assert_proxy_create_and_poll(self, workflow_id):
        public_model = f"autodl-public-{time.time_ns()}"
        is_zm = workflow_id in ZM_WORKFLOWS
        media = {
            "ref_image_0": "https://cdn.example/image.png",
            "ref_audio_0": "https://cdn.example/voice.wav",
        } if is_zm else {}
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
                "image_count": 9 if is_zm else 0,
                "supports_image": is_zm,
                "supports_video": False,
                "supports_audio": is_zm,
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
                        "status": "completed" if is_zm else "SUCCESS",
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
                **({
                    "image_urls": [media["ref_image_0"]],
                    "audio_urls": [media["ref_audio_0"]],
                } if is_zm else {}),
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
            **media,
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
