"""End-to-end check for the media-count rejection, run against a real server.

Boots the app with a scratch database, points one route at a stub upstream,
and posts requests through the public /v1/videos endpoint. Confirms that
over-limit uploads are refused with a reason and that in-budget uploads reach
the upstream unchanged, including the legacy single-video spelling.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PORT = 8811
STUB_PORT = 8812
ADAPTER_KEY = "e2e-adapter-key"
received: list[dict] = []


class StubUpstream(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        received.append(body)
        payload = json.dumps({"task_id": "e2e-task", "status": "queued"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


def post(path: str, payload: dict, headers: dict | None = None):
    request = urllib.request.Request(
        f"http://127.0.0.1:{APP_PORT}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="media-limit-e2e-")
    os.environ.update({
        "ADMIN_USERNAME": "admin",
        "ADMIN_PASSWORD": "e2e-password",
        "SESSION_SECRET": "e2e-session-secret-with-more-than-32-chars",
        "ADAPTER_API_KEY": ADAPTER_KEY,
        "ENCRYPTION_KEY": "IougsRYbjtzQcNSrzLV2O-TQ3k1PDP69XcfdR3Lxp3I=",
        "DATA_DIR": workdir,
    })
    stub = HTTPServer(("127.0.0.1", STUB_PORT), StubUpstream)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(APP_PORT)],
        cwd=REPO, env=dict(os.environ),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []
    try:
        for _ in range(60):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/healthz", timeout=3)
                break
            except OSError:
                continue
        else:
            raise SystemExit("app did not come up")

        sys.path.insert(0, REPO)
        from app import database  # 与真实服务共用 DATA_DIR 下的同一个库

        database.save_upstream({
            "name": "e2e",
            "base_url": f"http://127.0.0.1:{STUB_PORT}",
            "api_key": "stub-key",
            "enabled": True,
            "priority": 1,
            "routes": [{
                "model": "e2e-rolldek",
                "upstream_model": "sd-2.5-ch1",
                "protocol": "rolldek",
                "profile": "rolldek-sd25-ch1",
                "image_count": 2,
                "video_count": 3,
                "audio_count": 0,
            }],
        })

        def check(condition: bool, message: str) -> None:
            if not condition:
                failures.append(message)

        # 1) 视频超量 -> 400，说明超了几个，并且不向上游发请求。
        received.clear()
        status, body = post("/v1/videos", {
            "model": "e2e-rolldek", "prompt": "test",
            "reference_videos": [f"https://cdn/{i}.mp4" for i in range(4)],
        }, {"Authorization": f"Bearer {ADAPTER_KEY}"})
        check(status == 400, f"over-limit videos should be 400, got {status}")
        check(
            body.get("detail") == "视频数量超过上限：本次请求 4 个，当前模型最多 3 个",
            f"unexpected video detail: {body.get('detail')!r}",
        )
        check(not received, "a rejected request must not reach the upstream")

        # 2) 图片超量 -> 400。
        status, body = post("/v1/videos", {
            "model": "e2e-rolldek", "prompt": "test",
            "image_urls": ["https://cdn/1.png", "https://cdn/2.png", "https://cdn/3.png"],
        }, {"Authorization": f"Bearer {ADAPTER_KEY}"})
        check(status == 400, f"over-limit images should be 400, got {status}")
        check(
            body.get("detail") == "图片数量超过上限：本次请求 3 张，当前模型最多 2 张",
            f"unexpected image detail: {body.get('detail')!r}",
        )

        # 3) 配置为 0 = 不支持：传了就拒绝，理由要写“不支持”。
        status, body = post("/v1/videos", {
            "model": "e2e-rolldek", "prompt": "test",
            "audio_urls": ["https://cdn/voice.mp3"],
        }, {"Authorization": f"Bearer {ADAPTER_KEY}"})
        check(status == 400, f"unsupported audio should be 400, got {status}")
        check(
            body.get("detail") == "音频数量超过上限：本次请求 1 个，当前模型不支持音频",
            f"unexpected audio detail: {body.get('detail')!r}",
        )

        # 4) 旧格式单个 reference_video：在预算内，原样放进上游请求体。
        received.clear()
        status, _ = post("/v1/videos", {
            "model": "e2e-rolldek", "prompt": "test",
            "reference_video": "https://cdn/legacy.mp4",
        }, {"Authorization": f"Bearer {ADAPTER_KEY}"})
        check(status == 200, f"legacy single video should pass, got {status}")
        check(
            received and received[0].get("video_urls") == ["https://cdn/legacy.mp4"],
            f"legacy single video was not relayed as-is: {received!r}",
        )

        # 5) 三个类型都在预算内 -> 放行，且数量原样保留。
        received.clear()
        status, _ = post("/v1/videos", {
            "model": "e2e-rolldek", "prompt": "test",
            "image_urls": ["https://cdn/1.png", "https://cdn/2.png"],
            "reference_videos": ["https://cdn/1.mp4", "https://cdn/2.mp4", "https://cdn/3.mp4"],
        }, {"Authorization": f"Bearer {ADAPTER_KEY}"})
        check(status == 200, f"in-budget media should pass, got {status}")
        check(
            received and len(received[0].get("video_urls", [])) == 3
            and len(received[0].get("image_urls", [])) == 2,
            f"in-budget media was altered: {received!r}",
        )
    finally:
        server.terminate()
        stub.shutdown()
    if failures:
        print("FAILURES:")
        for item in failures:
            print(" -", item)
        return 1
    print("media limit end-to-end checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
