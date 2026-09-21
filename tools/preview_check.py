"""Assert the upstream dialog still reads its route values after the row rework.

The parameter inputs moved inside wrappers so the row can restack on a narrow
window. `readRoutes` collects them by field name, so this drives the real page
and compares what the dialog would send against what was stored, which is the
only way to catch a wrapper change that silently blanks a field.

Usage:
    python tools/preview_check.py --port 8791
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from preview_shot import WORK_DIR, DevTools, pick_chrome, sign_in  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--row", type=int, default=0)
    parser.add_argument("--width", type=int, default=1680)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--measure-overflow", action="store_true", help="fail when the route editor scrolls sideways")
    parser.add_argument("--username", default=os.environ.get("PREVIEW_USER", "admin"))
    parser.add_argument("--password", default=os.environ.get("PREVIEW_PASSWORD", ""))
    args = parser.parse_args()

    cookie = sign_in(args.port, args.username, args.password)
    chrome = subprocess.Popen(
        [
            pick_chrome(),
            "--headless=new",
            "--remote-debugging-port=9223",
            "--disable-gpu",
            "--no-first-run",
            f"--user-data-dir={os.path.join(WORK_DIR, 'chrome-profile-check')}",
            f"--window-size={args.width},{args.height}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    failures: list[str] = []
    try:
        target = None
        for _ in range(40):
            time.sleep(0.5)
            try:
                with urllib.request.urlopen("http://127.0.0.1:9223/json/list", timeout=3) as response:
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
        client.call(
            "Network.setCookie",
            name=cookie.split("=", 1)[0],
            value=cookie.split("=", 1)[1],
            url=f"http://127.0.0.1:{args.port}/",
        )
        client.call("Page.navigate", url=f"http://127.0.0.1:{args.port}/admin")
        time.sleep(3)
        client.call(
            "Emulation.setDeviceMetricsOverride",
            width=args.width,
            height=args.height,
            deviceScaleFactor=1,
            mobile=False,
        )
        time.sleep(0.5)
        client.evaluate(f"document.querySelectorAll('[data-edit]')[{args.row}].click()")
        time.sleep(0.8)

        stored = json.loads(
            client.evaluate("JSON.stringify({name: dashboard.upstreams[%d].name, routes: dashboard.upstreams[%d].routes})" % (args.row, args.row))
        )
        read = client.evaluate("JSON.stringify(readRoutes())")
        read = json.loads(read)

        print("stored routes:", len(stored["routes"]), "| read routes:", len(read))
        if len(stored["routes"]) != len(read):
            failures.append(f"route count changed: {len(stored['routes'])} -> {len(read)}")

        checked_fields = (
            "model", "upstream_model", "protocol", "profile", "durations",
            "resolutions", "image_count", "video_count", "supports_image",
            "supports_video", "supports_audio", "forward_resolution", "enabled",
        )
        for index, (expected, actual) in enumerate(zip(stored["routes"], read)):
            for field in checked_fields:
                want = expected.get(field)
                got = actual.get(field)
                if field == "durations":
                    want = sorted(want or [])
                    got = sorted(got or [])
                if field == "image_count" and want is None:
                    continue
                if want != got:
                    failures.append(f"row {index} field {field}: stored={want!r} read={got!r}")

        summary = client.evaluate("document.querySelectorAll('[data-route-params-summary]')[0].textContent")
        print("row 0 summary:", summary)
        if "图" not in (summary or ""):
            failures.append("summary is empty or missing the image count")

        if args.measure_overflow:
            overflow = client.evaluate(
                "(() => { const el = document.querySelector('.route-editor'); return el.scrollWidth - el.clientWidth; })()"
            )
            print("horizontal overflow:", overflow, "px")
            if overflow and overflow > 1:
                failures.append(f"route editor scrolls sideways by {overflow}px at width {args.width}")

        # Toggling must reveal the parameter row and flipping a checkbox there
        # must be reflected in what the dialog would save.
        client.evaluate("document.querySelectorAll('[data-route-params-toggle]')[0].click()")
        time.sleep(0.4)
        hidden = client.evaluate("document.querySelectorAll('[data-route-params]')[0].hidden")
        if hidden:
            failures.append("parameter row did not expand")
        client.evaluate(
            "(() => { const input = document.querySelectorAll('[data-route-field=\"video_count\"]')[0]; input.value = '7'; input.dispatchEvent(new Event('change', {bubbles: true})); })()"
        )
        time.sleep(0.3)
        saved_video_count = json.loads(client.evaluate("JSON.stringify(readRoutes())"))[0]["video_count"]
        if saved_video_count != 7:
            failures.append(f"video_count edit was not read back: {saved_video_count!r}")
        summary_after = client.evaluate("document.querySelectorAll('[data-route-params-summary]')[0].textContent")
        if "视7" not in (summary_after or ""):
            failures.append(f"summary did not follow the edit: {summary_after!r}")

        if failures:
            print("\nFAILURES:")
            for item in failures:
                print(" -", item)
            return 1
        print("\nall route checks passed")
        return 0
    finally:
        chrome.terminate()


if __name__ == "__main__":
    sys.exit(main())
