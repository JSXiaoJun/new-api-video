"""Scenario check: the three sdg-2.5 rows from the screenshot.

Boots nothing; it drives ``create_video`` against a stub upstream so the body
that would leave for MAI Token can be inspected per route.
"""
from __future__ import annotations

import asyncio
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
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="sdg-scenario-"))

import httpx
from fastapi import HTTPException

from app import database
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


def main() -> int:
    database.initialize()
    stamp = time.time_ns()
    rows = [
        (f"seedance-2.5-1080p-{stamp}", "sdg-2.5-1080p", "mai-token-1080p", ["1080p"]),
        (f"seedance-2.5-720p-{stamp}", "sdg-2.5-720p", "mai-token-720p", ["720p"]),
        (f"seedance-2.5-480p-{stamp}", "sdg-2.5-480p", "mai-token-480p", ["480p"]),
    ]
    database.save_upstream({
        "name": f"sdg-{stamp}",
        "base_url": "https://api.mai-token.com",
        "api_key": "secret",
        "enabled": True,
        "priority": 1,
        "routes": [
            {
                "model": public, "upstream_model": upstream, "protocol": "mai-token",
                "profile": profile, "resolutions": resolutions,
                "durations": list(range(4, 16)),
                "image_count": 9, "video_count": 3, "audio_count": 3,
            }
            for public, upstream, profile, resolutions in rows
        ],
    })

    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    # 1) Every row states its own tier upstream, with no caller hint at all.
    for public, upstream, profile, resolutions in rows:
        body = sent_body({"model": public, "prompt": "测试", "seconds": 5})
        check(body.get("resolution") == resolutions[0],
              f"{public}: upstream resolution was {body.get('resolution')!r}, wanted {resolutions[0]!r}")
        check(body.get("model") == upstream, f"{public}: upstream model was {body.get('model')!r}")
        print(f"{public:34} -> model={body.get('model'):18} resolution={body.get('resolution')}")

    # 2) A matching tier in a different spelling is normalized, not rejected.
    body = sent_body({"model": rows[0][0], "prompt": "测试", "resolution": "1080P"})
    check(body.get("resolution") == "1080p", f"720P spelling: {body.get('resolution')!r}")
    body = sent_body({"model": rows[1][0], "prompt": "测试", "size": "1280x720"})
    check(body.get("ratio") == "16:9", f"size ratio was dropped: {body.get('ratio')!r}")
    check(body.get("resolution") == "720p", f"size tier was {body.get('resolution')!r}")

    # 3) A tier the model does not publish is refused and never sent.
    for public, _, _, resolutions in rows:
        wrong = "480p" if resolutions[0] != "480p" else "1080p"
        with patch("app.proxy.httpx.AsyncClient") as client:
            try:
                sent_body({"model": public, "prompt": "测试", "resolution": wrong})
            except HTTPException as error:
                check(error.status_code == 400, f"{public}: status {error.status_code}")
                check(resolutions[0] in str(error.detail),
                      f"{public}: message does not name {resolutions[0]}: {error.detail}")
                check(wrong in str(error.detail), f"{public}: message does not name {wrong}: {error.detail}")
                client.assert_not_called()
                print(f"{public:34} + {wrong:6} -> 400 {error.detail}")
            else:
                failures.append(f"{public}: {wrong} was accepted")

    if failures:
        print("\nFAILURES:")
        for item in failures:
            print(" -", item)
        return 1
    print("\nscenario checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
