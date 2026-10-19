"""Measure how long the reference videos of a request really are.

Some routes bill reference videos by their length. The proxy reads only the
sample table of each MP4/MOV file (the ``moov`` box) through HTTP Range
requests, so the video itself is never downloaded or written to disk. The
length is the sum of the sample durations in the video track's ``stts`` table,
which is the timeline a player and the upstream actually follow. The single
duration numbers in ``mvhd`` / ``tkhd`` / ``mdhd`` are ignored: anyone can edit
them without changing what plays.

A video that cannot be measured this way (another container such as WebM or
MKV, fragmented MP4, a host that ignores Range, an oversized index, a private
address) is reported as ``None``; New API then keeps the reserved charge for it.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import httpx

from .config import settings

logger = logging.getLogger("uvicorn.error")

MAX_REDIRECTS = 5
# Top-level boxes walked before giving up on finding moov.
MAX_TOP_LEVEL_BOXES = 64
# The first request reads this much, which usually holds ftyp and, for
# web-optimized files, the whole moov.
FIRST_READ_BYTES = 64 * 1024
# Boxes that may open an MP4 or QuickTime file. Anything else (WebM starts
# with an EBML header) is another container.
LEADING_BOX_TYPES = {b"ftyp", b"styp", b"wide", b"free", b"skip", b"mdat", b"moov", b"pnot", b"uuid"}


class MediaProbeError(Exception):
    """A reference video could not be measured from its index."""


async def _resolve(host: str, port: int, **kwargs):
    return await asyncio.get_running_loop().getaddrinfo(host, port, **kwargs)


async def check_public_url(url: str) -> None:
    """Raise :class:`MediaProbeError` unless ``url`` reaches only public addresses."""
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
        raise MediaProbeError("not a public http(s) link")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise MediaProbeError("invalid port") from exc
    try:
        infos = await _resolve(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MediaProbeError("host does not resolve") from exc
    addresses = {info[4][0] for info in infos}
    # Every address must be public: the proxy must not be usable to reach its
    # own network through a user-supplied link.
    if not addresses or not all(ip_address(address.split("%", 1)[0]).is_global for address in addresses):
        raise MediaProbeError("host is not a public address")


class RangeReader:
    """Reads byte ranges of one remote file, following redirects safely."""

    def __init__(self, client: httpx.AsyncClient, url: str):
        self.client = client
        self.url = url
        self.size: int | None = None
        self.bytes_read = 0
        self._head = b""

    async def _get(self, start: int, end: int) -> bytes:
        wanted = end - start + 1
        url = self.url
        for _ in range(MAX_REDIRECTS + 1):
            await check_public_url(url)
            async with self.client.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}) as response:
                if response.is_redirect:
                    location = response.headers.get("Location")
                    if not location:
                        raise MediaProbeError("redirect without a location")
                    url = urljoin(url, location)
                    continue
                if response.status_code == 206:
                    total = response.headers.get("Content-Range", "").rpartition("/")[2]
                    if total.isdigit():
                        self.size = int(total)
                elif response.status_code == 200 and start == 0:
                    # The host ignored Range. Only a file no larger than one
                    # index read is accepted; anything bigger would be a full
                    # download, which this module never does.
                    declared = response.headers.get("Content-Length", "")
                    if not declared.isdigit() or int(declared) > settings.reference_video_index_max_bytes:
                        raise MediaProbeError("host does not support Range requests")
                    self.size = int(declared)
                    wanted = self.size
                else:
                    raise MediaProbeError(f"HTTP {response.status_code}")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk[: wanted - received])
                    received += len(chunks[-1])
                    if received >= wanted:
                        break
                self.url = url
                self.bytes_read += received
                return b"".join(chunks)
        raise MediaProbeError("too many redirects")

    async def read(self, start: int, length: int) -> bytes:
        if start + length <= len(self._head):
            return self._head[start : start + length]
        if self.size is not None and start + length > self.size:
            raise MediaProbeError("box runs past the end of the file")
        data = await self._get(start, start + length - 1)
        if len(data) < length:
            raise MediaProbeError("short read")
        return data

    async def open(self) -> None:
        self._head = await self._get(0, FIRST_READ_BYTES - 1)
        if self.size is None:
            self.size = len(self._head)


def _box_header(data: bytes, offset: int, end: int) -> tuple[bytes, int, int]:
    """Return (type, header length, box length) of the box at ``offset``."""
    if offset + 8 > end:
        raise MediaProbeError("truncated box header")
    size, box_type = struct.unpack_from(">I4s", data, offset)
    header = 8
    if size == 1:
        if offset + 16 > end:
            raise MediaProbeError("truncated box header")
        size = struct.unpack_from(">Q", data, offset + 8)[0]
        header = 16
    elif size == 0:
        size = end - offset
    if size < header or offset + size > end:
        raise MediaProbeError("invalid box size")
    return box_type, header, size


def _children(data: bytes, start: int, end: int) -> dict[bytes, list[tuple[int, int]]]:
    boxes: dict[bytes, list[tuple[int, int]]] = {}
    offset = start
    while offset < end:
        box_type, header, size = _box_header(data, offset, end)
        boxes.setdefault(box_type, []).append((offset + header, offset + size))
        offset += size
    return boxes


def _only(boxes: dict[bytes, list[tuple[int, int]]], box_type: bytes) -> tuple[int, int]:
    found = boxes.get(box_type)
    if not found:
        raise MediaProbeError(f"missing {box_type.decode()} box")
    return found[0]


def video_seconds_from_moov(moov: bytes) -> float:
    """Length of the longest video track, from its sample table.

    The longest one is taken because a player may choose any video track: a
    file with a one-frame decoy track next to the real one must not be billed
    by the decoy.
    """
    top = _children(moov, 0, len(moov))
    if b"mvex" in top:
        # Fragmented MP4 keeps its samples in moof boxes spread over the file.
        raise MediaProbeError("fragmented MP4")
    lengths: list[float] = []
    for trak_start, trak_end in top.get(b"trak", []):
        mdia = _children(moov, *_only(_children(moov, trak_start, trak_end), b"mdia"))
        hdlr_start, hdlr_end = _only(mdia, b"hdlr")
        if hdlr_end - hdlr_start < 12 or moov[hdlr_start + 8 : hdlr_start + 12] != b"vide":
            continue
        mdhd_start, mdhd_end = _only(mdia, b"mdhd")
        if mdhd_end <= mdhd_start:
            raise MediaProbeError("truncated mdhd")
        timescale_offset = mdhd_start + (20 if moov[mdhd_start] == 1 else 12)
        if timescale_offset + 4 > mdhd_end:
            raise MediaProbeError("truncated mdhd")
        timescale = struct.unpack_from(">I", moov, timescale_offset)[0]
        minf = _children(moov, *_only(mdia, b"minf"))
        stbl = _children(moov, *_only(minf, b"stbl"))
        stts_start, stts_end = _only(stbl, b"stts")
        if stts_end - stts_start < 8:
            raise MediaProbeError("truncated stts")
        entries = struct.unpack_from(">I", moov, stts_start + 4)[0]
        if stts_start + 8 + entries * 8 > stts_end:
            raise MediaProbeError("truncated stts")
        total = sum(
            count * delta
            for count, delta in struct.iter_unpack(">II", moov[stts_start + 8 : stts_start + 8 + entries * 8])
        )
        if timescale == 0 or total == 0:
            raise MediaProbeError("video track has no timed samples")
        lengths.append(total / timescale)
    if not lengths:
        raise MediaProbeError("no video track")
    return max(lengths)


async def _read_moov(reader: RangeReader) -> bytes:
    """Walk the top-level boxes by their headers and read only ``moov``."""
    await reader.open()
    size = reader.size or 0
    offset = 0
    for index in range(MAX_TOP_LEVEL_BOXES):
        if offset + 8 > size:
            break
        header = await reader.read(offset, min(16, size - offset))
        box_type = header[4:8]
        if index == 0 and box_type not in LEADING_BOX_TYPES:
            raise MediaProbeError("not an MP4/MOV file")
        # The header is parsed against the file size, not the 16 bytes read.
        declared = struct.unpack_from(">I", header, 0)[0]
        if declared == 1:
            if len(header) < 16:
                raise MediaProbeError("truncated box header")
            header_length, box_length = 16, struct.unpack_from(">Q", header, 8)[0]
        elif declared == 0:
            header_length, box_length = 8, size - offset
        else:
            header_length, box_length = 8, declared
        if box_length < header_length or offset + box_length > size:
            raise MediaProbeError("invalid box size")
        if box_type == b"moov":
            if box_length > settings.reference_video_index_max_bytes:
                raise MediaProbeError("video index is too large")
            return await reader.read(offset + header_length, box_length - header_length)
        offset += box_length
    raise MediaProbeError("no moov box")


async def _measure(url: str) -> float:
    async with httpx.AsyncClient(
        timeout=settings.reference_video_timeout_seconds, follow_redirects=False
    ) as client:
        moov = await _read_moov(RangeReader(client, url))
    return round(video_seconds_from_moov(moov), 3)


async def measure_video_seconds(url: str) -> float | None:
    """Seconds of one reference video, or None when it cannot be measured.

    The httpx timeout bounds each read, not the whole measurement, so a host
    trickling bytes is cut off by an overall deadline. Any failure stays local
    to this URL: the other videos of the request are still measured.
    """
    try:
        return await asyncio.wait_for(_measure(url), timeout=settings.reference_video_timeout_seconds)
    except asyncio.CancelledError:
        raise
    except Exception as error:  # Malformed URLs, hostnames and boxes all mean "not measurable".
        try:
            host = urlsplit(url).hostname
        except ValueError:
            host = None
        # The URL may carry a signature; log only the host.
        logger.info("Reference video not measured (%s): %s: %s", host, type(error).__name__, error)
        return None


async def measure_reference_videos(urls: list[str]) -> list[float | None]:
    """Measure each URL concurrently, keeping the request order."""
    return list(await asyncio.gather(*(measure_video_seconds(url) for url in urls)))
