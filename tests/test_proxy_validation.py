from __future__ import annotations

import unittest

from fastapi import HTTPException

from app.proxy import validate_online_image_inputs


class VideoImageInputValidationTests(unittest.TestCase):
    def test_rejects_data_uri_and_raw_base64_image_values(self):
        payloads = [
            {"image_url": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"},
            {"images": ["iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"]},
            {"image_url": {"url": "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQ"}},
            {"input_image": {"source": {"type": "base64", "data": "iVBORw0KGgoAAA"}}},
            {"image_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(HTTPException, "不支持 Base64"):
                    validate_online_image_inputs(payload)

    def test_accepts_online_urls_and_ignores_prompt_text(self):
        validate_online_image_inputs({
            "prompt": "不要把 data:image/png;base64,xxx 当成图片地址",
            "image_urls": ["https://cdn.example/image.png"],
            "images": [{"type": "image_url", "image_url": {"url": "http://cdn.example/other.jpg"}}],
        })

    def test_rejects_non_http_image_links(self):
        with self.assertRaisesRegex(HTTPException, "在线链接"):
            validate_online_image_inputs({"reference_images": ["file:///tmp/image.png"]})


if __name__ == "__main__":
    unittest.main()
