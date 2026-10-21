"""Replay the two configured routes exactly as the screenshot shows them."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "p")
os.environ.setdefault("SESSION_SECRET", "s" * 40)
os.environ.setdefault("ADAPTER_API_KEY", "k")
os.environ.setdefault("ENCRYPTION_KEY", "IougsRYbjtzQcNSrzLV2O-TQ3k1PDP69XcfdR3Lxp3I=")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fuyao-replay-"))

import httpx
from fastapi import HTTPException

from app import database
from app.model_profiles import capabilities_for
from app.proxy import create_video

ROUTES = [
    {
        "model": "seedance2.0-933-2", "upstream_model": "seedance2.0-933-2",
        "protocol": "fuyao", "profile": "fuyao-video",
        "durations": list(range(4, 16)), "resolutions": ["720p"],
        "image_count": 9, "video_count": 3, "audio_count": 3,
        "forward_resolution": True, "measure_reference_video": False, "enabled": True,
    },
    {
        "model": "seedance2.5-933-2", "upstream_model": "Seedance2.5xg",
        "protocol": "fuyao", "profile": "fuyao-video",
        "durations": list(range(4, 31)), "resolutions": ["720p"],
        "image_count": 9, "video_count": 3, "audio_count": 3,
        "forward_resolution": True, "measure_reference_video": False, "enabled": True,
    },
]


def sent_body(payload: dict) -> dict:
    captured: dict = {}

    class Stub:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            captured["json"] = kwargs["json"]
            return httpx.Response(
                200, request=httpx.Request("POST", url),
                json={"id": f"task_{time.time_ns()}", "status": "queued"},
            )

    with patch("app.proxy.httpx.AsyncClient", Stub):
        asyncio.run(create_video(payload, None))
    return captured["json"]


def main() -> int:
    database.initialize()
    stamp = time.time_ns()
    database.save_upstream({
        "name": f"replay-{stamp}", "base_url": "https://fuyao47.xyz",
        "api_key": "secret", "enabled": True, "priority": 1, "routes": ROUTES,
    })

    print("== Published capabilities ==")
    published = {item["id"]: item["capabilities"] for item in database.list_model_capabilities()}
    for route in ROUTES:
        caps = published.get(route["model"])
        print(f"{route['model']}: {json.dumps(caps, ensure_ascii=False)}")

    print("\n== Request bodies actually produced ==")
    cases = {
        "只给 prompt+时长": {"prompt": "海边日落", "duration": 5},
        "prompt+时长+分辨率": {"prompt": "海边日落", "duration": 5, "resolution": "720p"},
        "客户端传 size 1280x720": {"prompt": "海边日落", "duration": 5, "size": "1280x720"},
        "客户端传 size 1920x1080": {"prompt": "海边日落", "duration": 5, "size": "1920x1080"},
        "无声开关": {"prompt": "海边日落", "duration": 5, "generate_audio": False},
        "首帧": {"prompt": "转身", "duration": 5, "first_frame_url": "https://m/f.png"},
        "首尾帧": {"prompt": "过渡", "duration": 5, "first_frame_url": "https://m/f.png", "last_frame_url": "https://m/l.png"},
        "要 1080p": {"prompt": "海边日落", "duration": 5, "resolution": "1080p"},
    }
    for route in ROUTES:
        print(f"\n-- {route['model']} (upstream {route['upstream_model']}) --")
        for label, extra in cases.items():
            try:
                body = sent_body({"model": route["model"], **extra})
                print(f"  {label:24} -> {json.dumps(body, ensure_ascii=False)}")
            except HTTPException as error:
                print(f"  {label:24} -> 本地 400: {error.detail}")

    print("\n== Duration boundaries ==")
    for route in ROUTES:
        style = f"（4–{max(route['durations'])}s）"
        for value in (3, 4, 15, 16, 30, 31):
            try:
                body = sent_body({"model": route["model"], "prompt": "p", "duration": value})
                print(f"  {route['model']} {value:2}s -> 发送 seconds={body.get('seconds')!r}")
            except HTTPException as error:
                print(f"  {route['model']} {value:2}s -> 本地 400: {error.detail}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
