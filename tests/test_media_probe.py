from __future__ import annotations

import asyncio
import json
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx


os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-with-more-than-32-chars")
os.environ.setdefault("ADAPTER_API_KEY", "test-adapter-key")
os.environ.setdefault("ENCRYPTION_KEY", "IougsRYbjtzQcNSrzLV2O-TQ3k1PDP69XcfdR3Lxp3I=")
TEST_DATA_DIR = tempfile.TemporaryDirectory()
os.environ.setdefault("DATA_DIR", TEST_DATA_DIR.name)

from app import database, media_probe, proxy
from app.proxy import create_video, fetch_task


def box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


def full_box(box_type: bytes, payload: bytes) -> bytes:
    return box(box_type, b"\x00\x00\x00\x00" + payload)


def track(handler: bytes, timescale: int, samples: list[tuple[int, int]]) -> bytes:
    mdhd = full_box(b"mdhd", struct.pack(">IIII", 0, 0, timescale, 0) + b"\x00" * 4)
    hdlr = full_box(b"hdlr", b"\x00" * 4 + handler + b"\x00" * 12 + b"\x00")
    stts = full_box(b"stts", struct.pack(">I", len(samples)) + b"".join(struct.pack(">II", *s) for s in samples))
    stbl = box(b"stbl", stts)
    minf = box(b"minf", stbl)
    return box(b"trak", box(b"mdia", mdhd + hdlr + minf))


def mp4(seconds: int, *, moov_last: bool = False, edited_header: bool = False, mdat_bytes: int = 200_000) -> bytes:
    # 25 fps video track; mvhd claims a different length when edited_header is
    # set, which must not change the measurement.
    mvhd = full_box(b"mvhd", struct.pack(">IIII", 0, 0, 1000, 1 if edited_header else seconds * 1000) + b"\x00" * 80)
    audio = track(b"soun", 48000, [(seconds * 50, 960)])
    video = track(b"vide", 12800, [(seconds * 25, 512)])
    moov = box(b"moov", mvhd + audio + video)
    ftyp = box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    mdat = box(b"mdat", b"\x00" * mdat_bytes)
    return ftyp + (mdat + moov if moov_last else moov + mdat)


MEDIA = {
    "https://cdn.example/a.mp4": mp4(4),
    "https://cdn.example/b.mp4": mp4(15, moov_last=True, mdat_bytes=2_000_000),
    "https://cdn.example/edited.mp4": mp4(15, edited_header=True),
    "https://cdn.example/clip.webm": b"\x1a\x45\xdf\xa3" + b"\x00" * 1000,
    "https://cdn.example/fragmented.mp4": box(b"ftyp", b"isom") + box(b"moov", box(b"mvex", b"")),
    # A one-frame decoy video track ahead of the real 15 s one.
    "https://cdn.example/decoy.mp4": box(b"ftyp", b"isom")
    + box(b"moov", track(b"vide", 25, [(1, 1)]) + track(b"vide", 12800, [(15 * 25, 512)])),
    "https://cdn.example/empty-mdhd.mp4": box(b"ftyp", b"isom")
    + box(b"moov", box(b"trak", box(b"mdia", full_box(b"hdlr", bytes(4) + b"vide" + bytes(13)) + box(b"mdhd", b"")))),
}
NO_RANGE = {"https://norange.example/a.mp4", "https://norange.example/b.mp4"}


class StreamResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]):
        self.status_code = status
        self.headers = httpx.Headers(headers)
        self.is_redirect = status in {301, 302, 303, 307, 308}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def aiter_bytes(self):
        for start in range(0, len(self._body), 4096):
            yield self._body[start : start + 4096]


def media_client(captured: dict, *, redirect_to: str | None = None, upstream_status: str = "processing"):
    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, headers=None, **_kwargs):
            captured.setdefault("ranges", []).append((url, (headers or {}).get("Range")))
            if url == "https://cdn.example/redirect.mp4":
                return StreamResponse(302, b"", {"Location": redirect_to or ""})
            name = url.replace("https://norange.example/", "https://cdn.example/")
            body = MEDIA.get(name)
            if body is None:
                return StreamResponse(404, b"", {})
            if url in NO_RANGE:
                return StreamResponse(200, body, {"Content-Length": str(len(body))})
            start, _, end = headers["Range"].removeprefix("bytes=").partition("-")
            chunk = body[int(start) : int(end) + 1]
            content_range = f"bytes {start}-{int(start) + len(chunk) - 1}/{len(body)}"
            return StreamResponse(206, chunk, {"Content-Range": content_range, "Content-Length": str(len(chunk))})

        async def post(self, url, **kwargs):
            captured["post"] = (url, kwargs)
            return httpx.Response(200, request=httpx.Request("POST", url), json={"id": captured["task_id"], "status": "queued"})

        async def get(self, url, **kwargs):
            return httpx.Response(200, request=httpx.Request("GET", url), json={"status": upstream_status, "video_url": "https://out.example/v.mp4"})

    return Client


async def fake_dns(host, port, **_kwargs):
    address = "10.0.0.5" if host == "internal.example" else "93.184.216.34"
    return [(2, 1, 6, "", (address, port))]


def measure(url: str, captured: dict | None = None) -> float | None:
    with patch("app.media_probe.httpx.AsyncClient", media_client(captured if captured is not None else {})):
        return asyncio.run(media_probe.measure_video_seconds(url))


class MeasureTests(unittest.TestCase):
    def setUp(self):
        dns = patch("app.media_probe._resolve", fake_dns)
        dns.start()
        self.addCleanup(dns.stop)

    def test_index_at_the_start_is_measured_from_the_first_read(self):
        captured: dict = {}
        self.assertEqual(measure("https://cdn.example/a.mp4", captured), 4.0)
        self.assertEqual(len(captured["ranges"]), 1)

    def test_index_at_the_end_is_found_without_reading_the_video_data(self):
        captured: dict = {}
        self.assertEqual(measure("https://cdn.example/b.mp4", captured), 15.0)
        # The first 64 KB read, then box headers and moov; never the 2 MB mdat.
        read = sum(int(r.split("-")[1]) - int(r.split("=")[1].split("-")[0]) + 1 for _, r in captured["ranges"])
        self.assertLess(read, 100_000)

    def test_edited_header_duration_does_not_change_the_measurement(self):
        self.assertEqual(measure("https://cdn.example/edited.mp4"), 15.0)

    def test_unmeasurable_videos_return_none(self):
        for url in (
            "https://cdn.example/clip.webm",
            "https://cdn.example/fragmented.mp4",
            "https://cdn.example/missing.mp4",
            "https://internal.example/a.mp4",
        ):
            with self.subTest(url=url):
                self.assertIsNone(measure(url))

    def test_redirect_to_a_private_address_is_not_followed(self):
        captured: dict = {}
        with patch("app.media_probe.httpx.AsyncClient", media_client(captured, redirect_to="https://internal.example/a.mp4")):
            self.assertIsNone(asyncio.run(media_probe.measure_video_seconds("https://cdn.example/redirect.mp4")))
        self.assertEqual([url for url, _ in captured["ranges"]], ["https://cdn.example/redirect.mp4"])

    def test_host_without_range_support_is_not_downloaded_when_large(self):
        small = media_probe.settings.__class__(**{**media_probe.settings.__dict__, "reference_video_index_max_bytes": 1024 * 1024})
        with patch("app.media_probe.settings", small):
            self.assertIsNone(measure("https://norange.example/b.mp4"))

    def test_small_file_on_a_host_without_range_support_is_measured(self):
        self.assertEqual(measure("https://norange.example/a.mp4"), 4.0)

    def test_decoy_track_does_not_shorten_the_measurement(self):
        self.assertEqual(measure("https://cdn.example/decoy.mp4"), 15.0)

    def test_malformed_input_is_unmeasured_without_raising(self):
        for url in ("http://[::1/x", "https://" + "a" * 70 + ".example/v.mp4", "https://cdn.example/empty-mdhd.mp4"):
            with self.subTest(url=url):
                self.assertIsNone(measure(url))

    def test_one_bad_url_does_not_affect_the_other_videos(self):
        with patch("app.media_probe.httpx.AsyncClient", media_client({})):
            seconds = asyncio.run(media_probe.measure_reference_videos(["http://[::1/x", "https://cdn.example/a.mp4"]))
        self.assertEqual(seconds, [None, 4.0])

    def test_a_trickling_host_is_cut_off_by_the_overall_deadline(self):
        async def slow(_url):
            await asyncio.sleep(3600)

        quick = media_probe.settings.__class__(**{**media_probe.settings.__dict__, "reference_video_timeout_seconds": 0.05})
        with patch("app.media_probe.settings", quick), patch("app.media_probe._measure", slow):
            self.assertIsNone(asyncio.run(media_probe.measure_video_seconds("https://cdn.example/a.mp4")))

    def test_oversized_index_is_not_read(self):
        small = media_probe.settings.__class__(**{**media_probe.settings.__dict__, "reference_video_index_max_bytes": 64})
        with patch("app.media_probe.settings", small):
            self.assertIsNone(measure("https://cdn.example/b.mp4"))


class TaskFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        database.initialize()
        database.save_upstream(
            {
                "name": "wan-upstream",
                "base_url": "https://wan-upstream.example",
                "api_key": "key",
                "enabled": True,
                "priority": 1,
                "routes": [
                    {"model": "wan-measured", "protocol": "videos", "video_count": 5, "measure_reference_video": True},
                    {"model": "wan-plain", "protocol": "videos", "video_count": 5},
                ],
            }
        )

    def setUp(self):
        dns = patch("app.media_probe._resolve", fake_dns)
        dns.start()
        self.addCleanup(dns.stop)

    def run_flow(self, task_id: str, payload: dict, *, upstream_status: str = "completed", wait: bool = True):
        captured: dict = {"task_id": task_id}
        client = media_client(captured, upstream_status=upstream_status)

        async def flow():
            with patch("app.media_probe.httpx.AsyncClient", client), patch("app.proxy.httpx.AsyncClient", client):
                created = json.loads((await create_video(payload, None)).body)
                job = proxy._reference_video_measurements.get(task_id)
                if wait and job is not None:
                    await job
                polled = json.loads((await fetch_task(task_id)).body)
                if job is not None and not job.done():
                    job.cancel()
                return created, polled

        created, polled = asyncio.run(flow())
        return created, polled, captured

    def test_upstream_is_called_before_any_video_is_read(self):
        captured: dict = {"task_id": "t_order"}
        client = media_client(captured)
        events: list[str] = []
        original_post = client.post

        async def post(self, url, **kwargs):
            events.append("upstream")
            return await original_post(self, url, **kwargs)

        client.post = post

        async def flow():
            with patch("app.media_probe.httpx.AsyncClient", client), patch("app.proxy.httpx.AsyncClient", client):
                await create_video({"model": "wan-measured", "prompt": "p", "reference_video": "https://cdn.example/a.mp4"}, None)
                events.append("created")
                await proxy._reference_video_measurements["t_order"]

        asyncio.run(flow())
        self.assertEqual(events, ["upstream", "created"])
        self.assertIn("ranges", captured)

    def test_completed_poll_reports_each_measured_video(self):
        created, polled, _ = self.run_flow(
            "t_two",
            {"model": "wan-measured", "prompt": "p", "reference_videos": ["https://cdn.example/a.mp4", "https://cdn.example/b.mp4"]},
        )
        self.assertNotIn("usage", created)
        self.assertEqual(polled["status"], "completed")
        self.assertEqual(polled["usage"], {"input_video_seconds": 19.0, "reference_video_seconds": [4.0, 15.0]})

    def test_unmeasurable_video_is_reported_as_null(self):
        _, polled, _ = self.run_flow(
            "t_webm",
            {"model": "wan-measured", "prompt": "p", "reference_videos": ["https://cdn.example/a.mp4", "https://cdn.example/clip.webm"]},
        )
        self.assertEqual(polled["usage"], {"input_video_seconds": 4.0, "reference_video_seconds": [4.0, None]})

    def test_completion_waits_until_the_videos_are_measured(self):
        async def never(_urls):
            await asyncio.sleep(3600)

        with patch("app.media_probe.measure_reference_videos", never):
            _, polled, _ = self.run_flow(
                "t_slow", {"model": "wan-measured", "prompt": "p", "reference_video": "https://cdn.example/a.mp4"}, wait=False
            )
        self.assertEqual(polled["status"], "processing")
        self.assertNotIn("usage", polled)
        self.assertNotIn("video_url", polled)

    def test_lost_measurement_is_restarted_by_the_next_completed_poll(self):
        captured: dict = {"task_id": "t_restart"}
        client = media_client(captured, upstream_status="completed")

        async def flow():
            with patch("app.media_probe.httpx.AsyncClient", client), patch("app.proxy.httpx.AsyncClient", client):
                await create_video({"model": "wan-measured", "prompt": "p", "reference_video": "https://cdn.example/a.mp4"}, None)
                # Simulate a restart: the background job is gone before it ran.
                proxy._reference_video_measurements.pop("t_restart").cancel()
                first = json.loads((await fetch_task("t_restart")).body)
                await proxy._reference_video_measurements["t_restart"]
                second = json.loads((await fetch_task("t_restart")).body)
                return first, second

        first, second = asyncio.run(flow())
        self.assertEqual(first["status"], "processing")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(second["usage"]["reference_video_seconds"], [4.0])

    def test_route_table_rebuild_keeps_the_measure_flag(self):
        # A separate database: the rebuild must not disturb the shared one.
        with tempfile.TemporaryDirectory() as directory, patch.object(
            database, "DB_PATH", Path(directory) / "rebuild.db"
        ):
            database.initialize()
            database.save_upstream({
                "name": "rebuild", "base_url": "https://rebuild.example", "api_key": "k", "enabled": True,
                "priority": 1, "routes": [
                    {"model": "rebuild-measured", "protocol": "videos", "measure_reference_video": True},
                    {"model": "rebuild-plain", "protocol": "videos"},
                ],
            })
            with database.connection() as conn:
                # A CHECK list missing a protocol makes initialize() rebuild the table.
                sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'model_routes'").fetchone()["sql"]
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("DROP INDEX idx_model_routes_model")
                conn.execute("ALTER TABLE model_routes RENAME TO model_routes_old")
                conn.execute(sql.replace(", 'fuyao'", ""))
                conn.execute("INSERT INTO model_routes SELECT * FROM model_routes_old")
                conn.execute("DROP TABLE model_routes_old")
            database.initialize()
            self.assertTrue(database.select_upstream("rebuild-measured")["measure_reference_video"])
            self.assertFalse(database.select_upstream("rebuild-plain")["measure_reference_video"])

    def test_measured_route_without_reference_video_reports_zero(self):
        _, polled, _ = self.run_flow("t_none", {"model": "wan-measured", "prompt": "p"})
        self.assertEqual(polled["usage"], {"input_video_seconds": 0, "reference_video_seconds": []})

    def test_unmeasured_route_neither_reads_videos_nor_reports_usage(self):
        _, polled, captured = self.run_flow(
            "t_plain", {"model": "wan-plain", "prompt": "p", "reference_video": "https://cdn.example/a.mp4"}
        )
        self.assertNotIn("usage", polled)
        self.assertNotIn("ranges", captured)


if __name__ == "__main__":
    unittest.main()
