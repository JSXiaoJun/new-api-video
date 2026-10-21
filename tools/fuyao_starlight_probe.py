"""What a purely admin-configured route sends to Fuyao for the two models.

Builds the route the way the admin page would (profile ``fuyao-video``, the
generic fallback), then drives ``create_video`` against a stub upstream and
prints the body that would leave for Fuyao. Run:

    python tools/fuyao_starlight_probe.py
"""
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
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="fuyao-starlight-"))

import httpx
from fastapi import HTTPException

from app import database
from app.model_profiles import suggest_route
from app.proxy import create_video


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


MODELS = ("seedance2.0-933-2", "Seedance2.5xg")


def main() -> int:
    database.initialize()
    stamp = time.time_ns()

    # Exactly what 「同步上游模型」 would fill in for these names.
    discovered = {}
    for model in MODELS:
        route = suggest_route(model, "fuyao")
        discovered[model] = route
        print(f"{model}: suggested profile={route['profile']!r} "
              f"durations={route['durations']} resolutions={route['resolutions']} "
              f"images={route['image_count']} videos={route['video_count']} audios={route['audio_count']}")

    # Save the routes as the operator would after widening the media counts,
    # because the generic defaults only allow one of each.
    database.save_upstream({
        "name": f"fuyao-starlight-{stamp}",
        "base_url": "https://fuyao47.xyz",
        "api_key": "secret",
        "enabled": True,
        "priority": 1,
        "routes": [
            {
                "model": model,
                "upstream_model": model,
                "protocol": "fuyao",
                "profile": "fuyao-video",
                "durations": [5, 10],
                "resolutions": ["480p", "720p", "1080p"],
                "image_count": 3,
                "video_count": 3,
                "audio_count": 3,
            }
            for model in MODELS
        ],
    })

    db = database.list_upstreams()[0]["routes"]
    by_model = {row["model"]: row for row in db}
    print("\nSaved route (as the console stores it):")
    for model in MODELS:
        row = by_model[model]
        print(f"  {model}: profile={row['profile']} resolutions={row['resolutions']} "
              f"images={row['image_count']} forward_resolution={row['forward_resolution']}")

    scenarios = {
        "文生 (duration+resolution+aspect)": {
            "prompt": "海边日落，镜头缓慢向前推进",
            "duration": 5,
            "resolution": "720p",
            "aspect_ratio": "16:9",
        },
        "文生 + 无声开关": {
            "prompt": "海边日落",
            "duration": 5,
            "resolution": "720p",
            "generate_audio": False,
        },
        "首帧图生 (first_frame_url)": {
            "prompt": "人物缓缓转身",
            "duration": 5,
            "resolution": "720p",
            "first_frame_url": "https://media.example.com/first.png",
        },
        "首尾帧 (first+last)": {
            "prompt": "从首帧过渡到尾帧",
            "duration": 5,
            "resolution": "720p",
            "first_frame_url": "https://media.example.com/first.png",
            "last_frame_url": "https://media.example.com/last.png",
        },
        "多素材参考 (图+视频+音频)": {
            "prompt": "结合图片1与音频1生成",
            "duration": 5,
            "resolution": "720p",
            "image_urls": ["https://media.example.com/a.png"],
            "reference_videos": ["https://media.example.com/v.mp4"],
            "audio_urls": ["https://media.example.com/a.mp3"],
        },
        "客户端带像素 size": {
            "prompt": "海边日落",
            "duration": 5,
            "size": "1280x720",
        },
        "客户端带 mode": {
            "prompt": "海边日落",
            "duration": 5,
            "mode": "text2video",
        },
        "客户端要 4k": {
            "prompt": "海边日落",
            "duration": 5,
            "resolution": "4k",
        },
    }

    model = MODELS[0]
    print(f"\nOutgoing bodies for {model}:")
    for label, extra in scenarios.items():
        try:
            body = sent_body({"model": model, **extra})
            print(f"\n  {label}\n    {json.dumps(body, ensure_ascii=False)}")
        except HTTPException as error:
            print(f"\n  {label}\n    -> 本地拒绝 HTTP {error.status_code}: {error.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
