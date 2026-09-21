"""Drive the upstream dialog and assert its behaviour on the real page.

The route editor reads its values by field name and stores per-row state in data
attributes, so a markup change can silently drop a value while every unit test
still passes. This drives the actual page in a headless Chrome and checks the
paths an operator takes: opening a stored route, editing parameters while the
row is collapsed, expanding and collapsing, and adding and removing rows.

Clicks target the element a user would actually hit -- the summary text or the
caret inside the toggle, not just the button itself -- because `event.target` is
the innermost element and a handler written against `matches()` answers only for
the button.

Usage:
    python tools/preview_check.py --serve --seed --port 8791
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
from preview_shot import (  # noqa: E402
    WORK_DIR,
    SEED_ROUTES,
    DevTools,
    pick_chrome,
    seed_routes,
    sign_in,
    start_server,
)

CHECKED_FIELDS = (
    "model",
    "upstream_model",
    "protocol",
    "profile",
    "durations",
    "resolutions",
    "image_count",
    "video_count",
    "audio_count",
    "forward_resolution",
    "enabled",
)


class Checker:
    def __init__(self, client: DevTools) -> None:
        self.client = client
        self.failures: list[str] = []

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            self.failures.append(message)

    def evaluate(self, expression: str):
        return self.client.evaluate(expression)

    def json(self, expression: str):
        return json.loads(self.client.evaluate(f"JSON.stringify({expression})"))

    def click(self, selector: str) -> None:
        """Click the first element matching a CSS selector."""
        self.click_expr(f"document.querySelector({json.dumps(selector)})", selector)

    def click_expr(self, expression: str, label: str) -> None:
        """Click whatever an expression evaluates to, then let the page settle."""
        clicked = self.evaluate(
            "(() => { const el = (%s); if (!el) return 'missing'; el.click(); return 'clicked'; })()" % expression
        )
        self.check(clicked == "clicked", f"could not click {label}: {clicked}")
        time.sleep(0.35)

    def click_point(self, selector: str) -> None:
        """Send a real mouse press at the centre of an element.

        `element.click()` dispatches straight at that node, which hides a handler
        that only answers for the element itself. A real press lands on whatever
        is topmost at those coordinates -- usually a child span -- so this is what
        proves the delegated handlers work for a user.
        """
        box = self.json(
            "(() => { const el = document.querySelector(%s); if (!el) return null; const r = el.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; })()"
            % json.dumps(selector)
        )
        if not box:
            self.failures.append(f"nothing to click at {selector}")
            return
        for kind in ("mousePressed", "mouseReleased"):
            self.client.call(
                "Input.dispatchMouseEvent",
                type=kind,
                x=box["x"],
                y=box["y"],
                button="left",
                clickCount=1,
            )
        time.sleep(0.35)


def compare_routes(checker: Checker, label: str, expected: list[dict], actual: list[dict]) -> None:
    checker.check(len(expected) == len(actual), f"{label}: route count {len(expected)} -> {len(actual)}")
    for index, (want, got) in enumerate(zip(expected, actual)):
        for field in CHECKED_FIELDS:
            left = want.get(field)
            right = got.get(field)
            if field == "durations":
                left = sorted(left or [])
                right = sorted(right or [])
            if field == "image_count" and left is None:
                continue
            checker.check(left == right, f"{label}: row {index} {field} stored={left!r} read={right!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--row", type=int, default=0)
    parser.add_argument("--width", type=int, default=1680)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--serve", action="store_true", help="start a server against a scratch database")
    parser.add_argument("--seed", action="store_true", help="create the sample upstream first")
    parser.add_argument("--measure-overflow", action="store_true", help="fail when the editor scrolls sideways")
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
            "--remote-debugging-port=9224",
            "--disable-gpu",
            "--no-first-run",
            f"--user-data-dir={os.path.join(WORK_DIR, 'chrome-profile-check')}",
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
                with urllib.request.urlopen("http://127.0.0.1:9224/json/list", timeout=3) as response:
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
        client.call(
            "Emulation.setDeviceMetricsOverride",
            width=args.width,
            height=args.height,
            deviceScaleFactor=1,
            mobile=False,
        )
        client.call("Page.navigate", url=f"http://127.0.0.1:{args.port}/admin")
        time.sleep(3)

        checker = Checker(client)
        checker.click_expr(f"document.querySelectorAll('[data-edit]')[{args.row}]", f"edit button {args.row}")

        stored = checker.json(
            "({name: dashboard.upstreams[%d].name, routes: dashboard.upstreams[%d].routes})"
            % (args.row, args.row)
        )
        read = checker.json("readRoutes()")
        print("stored routes:", len(stored["routes"]), "| read routes:", len(read))
        compare_routes(checker, "round-trip", stored["routes"], read)

        # Every row starts collapsed with a summary standing in for the values.
        hidden = checker.evaluate("document.querySelectorAll('[data-route-params]')[0].hidden")
        checker.check(hidden is True, "parameter row should start collapsed")
        summary = checker.evaluate("document.querySelectorAll('[data-route-params-summary]')[0].textContent")
        print("row 0 summary:", summary)
        checker.check("图" in (summary or ""), f"summary missing the image count: {summary!r}")

        # Clicking the text inside the toggle is what an operator does; the
        # handler must not depend on the click landing on the button itself.
        checker.click_point("[data-route-params-summary]")
        hidden = checker.evaluate("document.querySelectorAll('[data-route-params]')[0].hidden")
        checker.check(hidden is False, "clicking the summary text did not expand the row")
        checker.click_point(".route-params-caret")
        hidden = checker.evaluate("document.querySelectorAll('[data-route-params]')[0].hidden")
        checker.check(hidden is True, "clicking the caret did not collapse the row")

        # An edit made while collapsed has to reach the summary and the payload.
        checker.evaluate(
            "(() => { const input = document.querySelectorAll('[data-route-field=\"video_count\"]')[0]; input.value = '7'; input.dispatchEvent(new Event('change', {bubbles: true})); })()"
        )
        time.sleep(0.3)
        saved = checker.json("readRoutes()")
        checker.check(saved[0]["video_count"] == 7, f"video_count edit not read back: {saved[0]['video_count']!r}")
        summary = checker.evaluate("document.querySelectorAll('[data-route-params-summary]')[0].textContent")
        checker.check("视7" in (summary or ""), f"summary did not follow the edit: {summary!r}")
        # Later comparisons must expect the edit, not the value it replaced.
        expected = [dict(route) for route in stored["routes"]]
        expected[0]["video_count"] = 7

        # 留空与 0 同义（不支持），摘要也必须如实说出来，不能只留一个数字。
        checker.evaluate(
            "(() => { const input = document.querySelectorAll('[data-route-field=\"audio_count\"]')[0]; input.value = ''; input.dispatchEvent(new Event('change', {bubbles: true})); })()"
        )
        time.sleep(0.3)
        saved = checker.json("readRoutes()")
        checker.check(saved[0]["audio_count"] == 0, f"blank audio count should read back as 0: {saved[0]['audio_count']!r}")
        summary = checker.evaluate("document.querySelectorAll('[data-route-params-summary]')[0].textContent")
        checker.check("音不支持" in (summary or ""), f"summary did not report unsupported audio: {summary!r}")
        expected[0]["audio_count"] = 0

        # The duration picker is a button holding a summary string, and its menu
        # is rendered by a delegated handler, so drive it through the real page.
        # The row was just collapsed, and a hidden panel has no clickable
        # coordinates, so open it again first.
        checker.click_point("[data-route-params-summary]")
        checker.click_point(".duration-picker")
        menu_open = checker.evaluate("document.querySelectorAll('.duration-menu').length")
        checker.check(menu_open == 1, f"duration picker did not open its menu: {menu_open} menus")
        checker.evaluate(
            "(() => { const input = document.querySelector('[data-duration-input]'); input.value = '9'; document.querySelector('[data-duration-add]').click(); })()"
        )
        time.sleep(0.3)
        durations = checker.json("readRoutes()")[0]["durations"]
        checker.check(9 in durations, f"duration 9 was not added: {durations}")
        checker.check("9s" in (checker.evaluate("document.querySelectorAll('[data-route-params-summary]')[0].textContent") or ""), "summary did not pick up the new duration")
        expected[0]["durations"] = sorted({*expected[0].get("durations", []), 9})
        checker.evaluate("document.body.click()")
        time.sleep(0.3)

        # The bulk toggle has to agree with the rows it drives.
        checker.click("#toggle-route-params")
        states = checker.json("[...document.querySelectorAll('[data-route-params]')].map((el) => el.hidden)")
        checker.check(all(state is False for state in states), f"expand-all left rows collapsed: {states}")
        label = checker.evaluate("document.querySelector('#toggle-route-params').textContent")
        checker.check("收起" in (label or ""), f"bulk button label out of sync: {label!r}")
        checker.click("#toggle-route-params")
        states = checker.json("[...document.querySelectorAll('[data-route-params]')].map((el) => el.hidden)")
        checker.check(all(state is True for state in states), f"collapse-all left rows open: {states}")

        # Adding and removing rows must keep the toolbar label honest and must
        # not disturb the values already entered.
        before = len(checker.json("readRoutes()"))
        checker.click("#add-route")
        checker.evaluate(
            "(() => { const rows = document.querySelectorAll('.route-editor-row'); const input = rows[rows.length - 1].querySelector('[data-route-field=\"model\"]'); input.value = 'added-by-check'; input.dispatchEvent(new Event('input', {bubbles: true})); })()"
        )
        rows_now = checker.json("[...document.querySelectorAll('.route-editor-row')].length")
        checker.check(rows_now == before + 1, f"add route did not append a row: {before} -> {rows_now}")
        checker.click(".route-editor-row:last-child .route-remove")
        rows_now = checker.json("[...document.querySelectorAll('.route-editor-row')].length")
        checker.check(rows_now == before, f"remove route left a row behind: {rows_now}")
        after = checker.json("readRoutes()")
        compare_routes(checker, "after add and remove", expected, after)

        # Rebuilding the rows (what synchronization does) resets them to
        # collapsed, and the toolbar label has to follow.
        checker.evaluate("setRouteRows(readRoutes(true, true))")
        time.sleep(0.4)
        states = checker.json("[...document.querySelectorAll('[data-route-params]')].map((el) => el.hidden)")
        checker.check(all(state is True for state in states), f"rebuilt rows should start collapsed: {states}")
        label = checker.evaluate("document.querySelector('#toggle-route-params').textContent")
        checker.check("展开" in (label or ""), f"label out of sync after rebuild: {label!r}")

        # The saved payload has to survive a real submit and read back unchanged.
        checker.evaluate("document.querySelector('#upstream-form').requestSubmit()")
        time.sleep(2.5)
        checker.evaluate("location.reload()")
        time.sleep(3)
        checker.click_expr(f"document.querySelectorAll('[data-edit]')[{args.row}]", f"edit button {args.row} after reload")
        reloaded = checker.json("readRoutes()")
        compare_routes(checker, "after save and reload", expected, reloaded)

        if args.measure_overflow:
            overflow = checker.evaluate(
                "(() => { const el = document.querySelector('.route-editor'); return el.scrollWidth - el.clientWidth; })()"
            )
            print("horizontal overflow:", overflow, "px")
            checker.check(not overflow or overflow <= 1, f"editor scrolls sideways by {overflow}px at width {args.width}")

        if checker.failures:
            print("\nFAILURES:")
            for item in checker.failures:
                print(" -", item)
            return 1
        print("\nall route editor checks passed")
        return 0
    finally:
        chrome.terminate()


if __name__ == "__main__":
    sys.exit(main())
