from __future__ import annotations

import unittest

from app.ark_video import extract_task_fields, has_reference_content, task_path, transform_create_payload


class ArkVideoTests(unittest.TestCase):
    def test_transforms_canonical_multimodal_payload(self):
        payload = transform_create_payload({
            "model": "doubao-seedance-2-0-260128",
            "prompt": "保持角色一致",
            "aspect_ratio": "16:9",
            "duration": 15,
            "resolution": "720p",
            "generate_audio": True,
            "image_urls": ["https://cdn.example/one.png", "https://cdn.example/two.png"],
            "reference_videos": ["https://cdn.example/one.mp4", "https://cdn.example/two.mp4"],
            "audio_urls": ["https://cdn.example/one.mp3"],
        })

        self.assertEqual(payload, {
            "model": "doubao-seedance-2-0-260128",
            "content": [
                {"type": "text", "text": "保持角色一致"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://cdn.example/one.png"},
                    "role": "reference_image",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "https://cdn.example/two.png"},
                    "role": "reference_image",
                },
                {
                    "type": "video_url",
                    "video_url": {"url": "https://cdn.example/one.mp4"},
                    "role": "reference_video",
                },
                {
                    "type": "video_url",
                    "video_url": {"url": "https://cdn.example/two.mp4"},
                    "role": "reference_video",
                },
                {
                    "type": "audio_url",
                    "audio_url": {"url": "https://cdn.example/one.mp3"},
                    "role": "reference_audio",
                },
            ],
            "ratio": "16:9",
            "duration": 15,
            "resolution": "720p",
            "generate_audio": True,
        })

    def test_accepts_single_reference_aliases_without_prompt(self):
        payload = {
            "model": "doubao-seedance-2-0-260128",
            "prompt": "",
            "image_url": "https://cdn.example/main.png",
            "reference_image_urls": ["https://cdn.example/reference.png"],
            "reference_video": "https://cdn.example/reference.mp4",
            "audio_url": "https://cdn.example/reference.mp3",
            "seconds": -1,
            "watermark": False,
        }

        self.assertTrue(has_reference_content(payload))
        transformed = transform_create_payload(payload)
        self.assertEqual(transformed["duration"], -1)
        self.assertFalse(transformed["watermark"])
        self.assertNotIn("aspect_ratio", transformed)
        self.assertEqual([item["role"] for item in transformed["content"]], [
            "reference_image",
            "reference_image",
            "reference_video",
            "reference_audio",
        ])

    def test_extracts_native_task_fields_and_encodes_task_path(self):
        self.assertEqual(
            extract_task_fields({
                "status": "succeeded",
                "content": {"video_url": "https://cdn.example/result.mp4"},
                "error": None,
            }),
            {
                "status": "succeeded",
                "video_url": "https://cdn.example/result.mp4",
                "error": None,
                "progress": None,
            },
        )
        self.assertEqual(
            task_path("cgt/task 1"),
            "/api/v3/contents/generations/tasks/cgt%2Ftask%201",
        )


if __name__ == "__main__":
    unittest.main()
