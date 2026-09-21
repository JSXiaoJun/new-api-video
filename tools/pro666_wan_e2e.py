"""End-to-end check for the Pro666 wan3.0 family, run against a real server.

Boots the app with a scratch database, points a Pro666 route at a stub upstream
that answers both ``GET /v1/models`` (discovery) and ``POST /v1/videos``
(creation). Confirms that a wan model discovered by name shape gets the
advertised media budget, that the reference arrays reach the upstream intact,
and that an over-budget request is refused before anything is sent upstream.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PORT = 8821
STUB_PORT = 8822
ADAPTER_KEY = "wan-e2e-adapter-key"
MODELS = [
    "wan3.0-480p",
    "wan3.0-720p",
    "wan3.0-1080p-prime",
    "wan3.0-720p-turbo",
    "sd2.5-720p",
]
created: list[dict] = []


class StubUpstream(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if not self.path.startswith("/v1/models"):
            self.send_error(404)
            return
        payload = json.dumps({"data": [{"id": name} for name in MODELS]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        created.append(json.loads(self.rfile.read(length) or b"{}"))
        payload = json.dumps({"task_id": "wan-e2e-task", "status": "queued"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


def post(path: str, payload: dict):
    request = urllib.request.Request(
        f"http://127.0.0.1:{APP_PORT}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {ADAPTER_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="pro666-wan-e2e-")
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

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

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
        from app import database
        from app.main import normalize_discovered_models

        # 0) 后台「同步上游模型」按钮走的就是这个接口：协议按模型名推导，
        #    wan 必须被识别成 videos + pro666-wan-* ，而不是通用的 default。
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        opener.open(
            urllib.request.Request(
                f"http://127.0.0.1:{APP_PORT}/admin/api/login",
                data=json.dumps({"username": "admin", "password": "e2e-password"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=10,
        )
        page = opener.open(f"http://127.0.0.1:{APP_PORT}/admin", timeout=10).read().decode()
        csrf = re.search(r'csrf-token" content="([^"]+)"', page).group(1)
        discovery = opener.open(
            urllib.request.Request(
                f"http://127.0.0.1:{APP_PORT}/admin/api/upstreams/models",
                data=json.dumps({"base_url": f"http://127.0.0.1:{STUB_PORT}", "api_key": "stub-key"}).encode(),
                headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
                method="POST",
            ),
            timeout=20,
        )
        admin_routes = {item["upstream_model"]: item for item in json.load(discovery)["models"]}
        check(admin_routes.get("wan3.0-720p", {}).get("profile") == "pro666-wan-720p",
              f"发现结果里的 wan3.0-720p 请求格式是 {admin_routes.get('wan3.0-720p', {}).get('profile')!r}")
        check(admin_routes.get("wan3.0-720p", {}).get("video_count") == 5,
              f"发现结果里的视频数量是 {admin_routes.get('wan3.0-720p', {}).get('video_count')!r}")

        # 下面复用同一份探测结果：与后台点「同步上游模型」拿到的是同一份数据。
        with urllib.request.urlopen(
            f"http://127.0.0.1:{STUB_PORT}/v1/models", timeout=10
        ) as response:
            discovered = normalize_discovered_models(json.load(response), "videos")
        routes = {route["upstream_model"]: route for route in discovered}
        check(len(routes) == len(MODELS), f"expected {len(MODELS)} discovered models, got {len(routes)}")
        for name, resolution in (
            ("wan3.0-480p", "pro666-wan-480p"),
            ("wan3.0-720p", "pro666-wan-720p"),
            ("wan3.0-1080p-prime", "pro666-wan-1080p"),
            ("wan3.0-720p-turbo", "pro666-wan-720p"),
        ):
            check(routes.get(name, {}).get("profile") == resolution, f"{name} -> {routes.get(name, {}).get('profile')}")
            check(routes.get(name, {}).get("video_count") == 5, f"{name} video_count {routes.get(name, {}).get('video_count')}")
            check(routes.get(name, {}).get("audio_count") == 5, f"{name} audio_count {routes.get(name, {}).get('audio_count')}")
            check(routes.get(name, {}).get("image_count") == 10, f"{name} image_count {routes.get(name, {}).get('image_count')}")

        saved = database.save_upstream({
            "name": "wan-e2e",
            "base_url": f"http://127.0.0.1:{STUB_PORT}",
            "api_key": "stub-key",
            "enabled": True,
            "priority": 1,
            "routes": [
                {
                    "model": route["upstream_model"],
                    "upstream_model": route["upstream_model"],
                    "protocol": route["protocol"],
                    "profile": route["profile"],
                    "durations": route["durations"],
                    "image_count": route["image_count"],
                    "video_count": route["video_count"],
                    "audio_count": route["audio_count"],
                }
                for route in discovered
            ],
        })

        # 1) 满预算的参考素材原样转发（5 视频 / 5 音频 / 10 图）。
        created.clear()
        status, _ = post("/v1/videos", {
            "model": "wan3.0-720p",
            "prompt": "参考 @Image1 @Video1 @Audio1",
            "duration": 10,
            "aspect_ratio": "9:16",
            "image_urls": [f"https://cdn/{i}.png" for i in range(10)],
            "reference_videos": [f"https://cdn/{i}.mp4" for i in range(5)],
            "audio_urls": [f"https://cdn/{i}.mp3" for i in range(5)],
        })
        check(status == 200, f"in-budget wan request should pass, got {status}")
        sent = created[0] if created else {}
        check(sent.get("model") == "wan3.0-720p", f"upstream model was {sent.get('model')!r}")
        check(len(sent.get("images") or []) == 10, f"images forwarded: {len(sent.get('images') or [])}")
        check(len(sent.get("videos") or []) == 5, f"videos forwarded: {len(sent.get('videos') or [])}")
        check(len(sent.get("audios") or []) == 5, f"audios forwarded: {len(sent.get('audios') or [])}")
        check(sent.get("duration") == 10 and sent.get("aspect_ratio") == "9:16", f"controls: {sent}")

        # 2) 第 6 个参考视频越界 -> 400，写明原因，且不向上游发请求。
        created.clear()
        status, body = post("/v1/videos", {
            "model": "wan3.0-720p",
            "prompt": "test",
            "reference_videos": [f"https://cdn/{i}.mp4" for i in range(6)],
        })
        check(status == 400, f"over-limit videos should be 400, got {status}")
        check(
            body.get("detail") == "视频数量超过上限：本次请求 6 个，当前模型最多 5 个",
            f"unexpected detail: {body.get('detail')!r}",
        )
        check(not created, "a rejected request must not reach the upstream")

        # 3) 旧格式单个 reference_video 仍然按单元素数组转发。
        created.clear()
        status, _ = post("/v1/videos", {
            "model": "wan3.0-480p",
            "prompt": "test",
            "reference_video": "https://cdn/legacy.mp4",
        })
        check(status == 200, f"legacy single video should pass, got {status}")
        check(
            (created[0] if created else {}).get("videos") == ["https://cdn/legacy.mp4"],
            f"legacy single video was not relayed as a one-element array: {created!r}",
        )

        # 4) 对外公布的能力与执行上限同源。
        with urllib.request.urlopen(
            f"http://127.0.0.1:{APP_PORT}/v1/model-capabilities", timeout=10
        ) as response:
            published = {item["id"]: item["capabilities"] for item in json.load(response)["data"]}
        wan = published.get("wan3.0-720p", {})
        check(wan.get("maxVideos") == 5, f"published maxVideos: {wan.get('maxVideos')}")
        check(wan.get("maxAudios") == 5, f"published maxAudios: {wan.get('maxAudios')}")
        check(wan.get("maxImages") == 10, f"published maxImages: {wan.get('maxImages')}")
        check(wan.get("durations") == list(range(5, 31)), f"published durations: {wan.get('durations')}")
        check(wan.get("resolutions") == ["720p"], f"published resolutions: {wan.get('resolutions')}")
        check(
            published.get("wan3.0-1080p-prime", {}).get("resolutions") == ["1080p"],
            "the -prime tier must publish the 1080p resolution",
        )
    finally:
        server.terminate()
        stub.shutdown()
    if failures:
        print("FAILURES:")
        for item in failures:
            print(" -", item)
        return 1
    print("pro666 wan end-to-end checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
