"""Screenshot the upstream dialog so the route editor can be reviewed visually.

The console is a single page with no build step, so the only way to check that
the routing grid, the collapsed parameter rows and the sticky column actually
line up is to render them. This drives a headless Chrome over the DevTools
protocol using the standard library only, because the repo has no browser
automation dependency and does not need one for a one-off check.

Usage:
    python tools/preview_shot.py [--port 8791] [--width 1680] [--out shot.png]
    python tools/preview_shot.py --serve --open-dialog --add-row

Log in first through the page is not needed: the dashboard reads the session
cookie, so the script signs in over HTTP and then loads the page with that
cookie already set.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

# Chrome profiles and the scratch database belong outside the repository, so a
# review run never leaves files that could be committed by accident.
WORK_DIR = os.path.join(tempfile.gettempdir(), "video-relay-preview")


def start_server(port: int) -> subprocess.Popen:
    """Start uvicorn against a scratch database and wait for it to answer.

    The preview must never touch `data/adapter.db`: it creates routes that are
    deleted again, and a left-over scratch row in the real database would look
    like a live upstream.
    """
    # Start from an empty database, otherwise a previous run's routes survive and
    # a check can pass against the wrong data.
    scratch = os.path.join(WORK_DIR, "data")
    shutil.rmtree(scratch, ignore_errors=True)
    os.makedirs(scratch, exist_ok=True)
    env = dict(os.environ)
    env["DATA_DIR"] = scratch
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=repo_root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(60):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/admin/login", timeout=3)
            return process
        except (urllib.error.URLError, OSError):
            continue
    process.terminate()
    raise SystemExit("preview server did not come up")


class DevTools:
    """Minimal DevTools protocol client over a raw websocket."""

    def __init__(self, url: str) -> None:
        self._sock = self._connect(url)
        self._next_id = 0

    @staticmethod
    def _connect(url: str) -> socket.socket:
        without_scheme = url.split("://", 1)[1]
        host_port, path = without_scheme.split("/", 1)
        host, port = host_port.split(":")
        sock = socket.create_connection((host, int(port)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode())
        header = b""
        while b"\r\n\r\n" not in header:
            header += sock.recv(4096)
        if b"101" not in header.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"websocket handshake failed: {header[:120]!r}")
        return sock

    def _send_frame(self, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x81])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _read_exact(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = self._sock.recv(count - len(data))
            if not chunk:
                raise RuntimeError("websocket closed")
            data += chunk
        return data

    def _read_frame(self) -> bytes:
        first, second = self._read_exact(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        if second & 0x80:
            mask = self._read_exact(4)
            payload = self._read_exact(length)
            return bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return self._read_exact(length)

    def call(self, method: str, **params) -> dict:
        self._next_id += 1
        message_id = self._next_id
        self._send_frame(json.dumps({"id": message_id, "method": method, "params": params}).encode())
        while True:
            message = json.loads(self._read_frame())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(f"{method} failed: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        return result.get("result", {}).get("value")


def pick_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    raise SystemExit("no Chrome or Edge binary found")


def sign_in(port: int, username: str, password: str) -> str:
    body = json.dumps({"username": username, "password": password}).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/admin/api/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.headers.get("Set-Cookie", "").split(";", 1)[0]


SEED_ROUTES = [
    {
        "model": "seedance-demo-720p",
        "upstream_model": "v1-seedance-2.0-720",
        "protocol": "videos",
        "profile": "pro666-v1-seedance-720p",
        "durations": [15],
        "resolutions": ["720p"],
        "image_count": 9,
        "video_count": 3,
        "supports_image": True,
        "supports_video": True,
        "supports_audio": True,
        "forward_resolution": True,
        "enabled": True,
    },
    {
        "model": "seedance-demo-mini",
        "upstream_model": "v1-seedance-2.0-mini",
        "protocol": "videos",
        "profile": "pro666-v1-seedance-mini-720p",
        "durations": [5, 6, 7, 8, 9, 10],
        "resolutions": [],
        "image_count": 9,
        "video_count": 12,
        "supports_image": True,
        "supports_video": True,
        "supports_audio": False,
        "forward_resolution": False,
        "enabled": False,
    },
]


def seed_routes(port: int, api_key: str, username: str, password: str) -> None:
    """Create one upstream holding both a plain and a disabled route."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open(
        urllib.request.Request(
            f"http://127.0.0.1:{port}/admin/api/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        ),
        timeout=10,
    )
    page = opener.open(f"http://127.0.0.1:{port}/admin", timeout=10).read().decode()
    csrf = re.search(r'csrf-token" content="([^"]+)"', page).group(1)
    payload = {
        "name": "预览上游",
        "priority": 100,
        "base_url": "https://pro666.top",
        "api_key": api_key,
        "enabled": True,
        "routes": SEED_ROUTES,
    }
    opener.open(
        urllib.request.Request(
            f"http://127.0.0.1:{port}/admin/api/upstreams",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf},
            method="POST",
        ),
        timeout=10,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--width", type=int, default=1680)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--out", default="preview.png")
    parser.add_argument("--path", default="/admin", help="console page to capture")
    parser.add_argument("--open-dialog", action="store_true")
    parser.add_argument("--edit-index", type=int, default=None, help="open the edit dialog of the Nth upstream row (0 based)")
    parser.add_argument("--expand", action="store_true")
    parser.add_argument("--scroll-editor", type=int, default=0, help="scroll the route editor sideways by N pixels before capturing")
    parser.add_argument("--add-row", action="store_true")
    parser.add_argument("--click", default=None, help="CSS selector to click before capturing")
    parser.add_argument("--serve", action="store_true", help="start a preview server against a scratch database")
    parser.add_argument("--seed", action="store_true", help="create two sample routes to render")
    parser.add_argument("--username", default=os.environ.get("PREVIEW_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("PREVIEW_PASSWORD", ""))
    args = parser.parse_args()

    server = start_server(args.port) if args.serve else None
    try:
        return run(args)
    finally:
        if server is not None:
            server.terminate()


def run(args) -> int:
    if args.seed:
        seed_routes(args.port, "preview-key", args.username, args.password)
    cookie = sign_in(args.port, args.username, args.password)
    chrome = subprocess.Popen(
        [
            pick_chrome(),
            "--headless=new",
            "--remote-debugging-port=9222",
            "--disable-gpu",
            "--no-first-run",
            f"--user-data-dir={os.path.join(WORK_DIR, 'chrome-profile')}",
            f"--window-size={args.width},{args.height}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        target = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen("http://127.0.0.1:9222/json/list", timeout=3) as response:
                    pages = [item for item in json.load(response) if item.get("type") == "page"]
                if pages:
                    target = pages[0]
                    break
            except (urllib.error.URLError, OSError):
                continue
        if not target:
            raise SystemExit("chrome devtools did not come up")

        client = DevTools(target["webSocketDebuggerUrl"])
        client.call("Page.enable")
        client.call("Runtime.enable")
        client.call("Network.enable")
        client.call("Network.setCookie", name=cookie.split("=", 1)[0], value=cookie.split("=", 1)[1], url=f"http://127.0.0.1:{args.port}/")
        client.call("Emulation.setDeviceMetricsOverride", width=args.width, height=args.height, deviceScaleFactor=1, mobile=False)
        client.call("Page.navigate", url=f"http://127.0.0.1:{args.port}{args.path}")
        time.sleep(3)
        if args.seed:
            client.evaluate("document.querySelectorAll('[data-edit]')[0].click()")
            time.sleep(0.8)
        elif args.open_dialog:
            client.evaluate("document.querySelector('#add-upstream').click()")
            time.sleep(0.6)
        if args.edit_index is not None:
            client.evaluate(
                f"document.querySelectorAll('[data-edit]')[{args.edit_index}].click()"
            )
            time.sleep(0.6)
        if args.add_row:
            client.evaluate("document.querySelector('#add-route').click()")
            time.sleep(0.4)
        if args.expand:
            client.evaluate("document.querySelector('#toggle-route-params').click()")
            time.sleep(0.4)
        if args.scroll_editor:
            client.evaluate(
                f"document.querySelector('.route-editor').scrollLeft = {args.scroll_editor}"
            )
            time.sleep(0.4)
        if args.click:
            client.evaluate(
                "(() => { const el = document.querySelector(%s); if (el) el.click(); })()" % json.dumps(args.click)
            )
            time.sleep(0.6)
        shot = client.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
        with open(args.out, "wb") as handle:
            handle.write(base64.b64decode(shot["data"]))
        print("saved", args.out)
        return 0
    finally:
        chrome.terminate()


if __name__ == "__main__":
    sys.exit(main())
if __name__ == "__main__":
    sys.exit(main())
