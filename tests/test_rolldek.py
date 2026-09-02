from __future__ import annotations

import unittest

from app.channels import rolldek


class RollDekAdapterTests(unittest.TestCase):
    def test_base_url_and_routes(self):
        self.assertTrue(rolldek.is_rolldek_base_url("https://rolldek.com"))
        self.assertTrue(rolldek.is_rolldek_base_url("https://api.rolldek.com/v1"))
        self.assertFalse(rolldek.is_rolldek_base_url("https://example.com"))
        self.assertEqual(rolldek.suggest_route("sd-2-ch3")["profile"], "rolldek-sd2-ch3")
        self.assertEqual(rolldek.suggest_route("sd-2.5-ch3")["durations"], [30])
        self.assertEqual(rolldek.suggest_route("sd-2.5-ch1-15s")["image_count"], 30)
        self.assertEqual(rolldek.suggest_route("sd-2.0-ch4")["image_count"], 9)

    def test_ch3_maps_images_and_fixed_duration(self):
        payload = rolldek.transform_create_payload({
            "model": "sd-2.5-ch3",
            "prompt": "@Image1 进入场景",
            "duration": 8,
            "aspect_ratio": "16:9",
            "image_urls": [f"https://cdn.example/{index}.png" for index in range(12)],
            "reference_video": "https://cdn.example/ignored.mp4",
        })
        self.assertEqual(payload["duration"], 30)
        self.assertEqual(len(payload["image_refs"]), 9)
        self.assertNotIn("reference_video", payload)

    def test_ch4_maps_multimodal_aliases_and_extracts_url(self):
        payload = rolldek.transform_create_payload({
            "model": "sd-2-ch4",
            "prompt": "参考 @Image1 @Video1 @Audio1",
            "seconds": "10",
            "generate_audio": True,
            "image_refs": ["https://cdn.example/a.png"],
            "video_refs": ["https://cdn.example/a.mp4"],
            "audio_refs": ["https://cdn.example/a.mp3"],
            "start_image_url": "https://cdn.example/start.png",
            "end_image_url": "https://cdn.example/end.png",
        })
        self.assertEqual(payload["duration"], "10")
        self.assertEqual(payload["generateAudio"], True)
        self.assertEqual(payload["images"], ["https://cdn.example/a.png"])
        self.assertEqual(payload["videos"], ["https://cdn.example/a.mp4"])
        self.assertEqual(payload["audios"], ["https://cdn.example/a.mp3"])
        self.assertEqual(payload["first_image"], "https://cdn.example/start.png")
        self.assertEqual(rolldek.extract_task_fields({"status": "completed", "url": "/result.mp4"})["video_url"], "/result.mp4")

    def test_ch1_fixed_duration_and_limits_are_isolated(self):
        payload = rolldek.transform_create_payload({
            "model": "sd-2.5-ch1-15s",
            "prompt": "参考多种素材",
            "seconds": 5,
            "size": "1280x720",
            "with_audio": False,
            "images": [f"https://cdn.example/{index}.png" for index in range(35)],
            "videos": [f"https://cdn.example/{index}.mp4" for index in range(12)],
            "audios": [f"https://cdn.example/{index}.mp3" for index in range(12)],
        })
        self.assertEqual(payload["duration"], 15)
        self.assertEqual(payload["aspect_ratio"], "16:9")
        self.assertFalse(payload["with_audio"])
        self.assertEqual(len(payload["image_urls"]), 30)
        self.assertEqual(len(payload["video_urls"]), 10)
        self.assertEqual(len(payload["audio_urls"]), 10)


if __name__ == "__main__":
    unittest.main()
