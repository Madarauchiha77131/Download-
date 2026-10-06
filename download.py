"""Download service: talks to the media-extraction backend and fetches the files.

Backend contract (same API the web UI uses)::

    GET  /api/server_status                  -> {"online": bool}
    POST /api/add {"url": ...}               -> {"task_id": "..."}
    GET  /api/status/<task_id>               -> {"status": "pending|processing|uploading|completed|failed|waking_up",
                                                  "queue_position": int, "error": str,
                                                  "files": [{"name", "link"}], "direct_link": str,
                                                  "thumbnail": str, "poster": str}
    GET  /api/proxy_download?url=&name=      -> streamed file
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import quote, urlsplit

import aiohttp

log = logging.getLogger("download")


# ── platforms ─────────────────────────────────────────────────────────────
class Platform(str, Enum):
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    OTHER = "other"

    @property
    def label(self) -> str:
        return {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram",
                "facebook": "Facebook", "other": "Web"}[self.value]


_HOST_MAP: tuple[tuple[Platform, tuple[str, ...]], ...] = (
    (Platform.YOUTUBE, ("youtube.com", "youtu.be", "youtube-nocookie.com")),
    (Platform.TIKTOK, ("tiktok.com", "tiktokv.com")),
    (Platform.INSTAGRAM, ("instagram.com", "instagr.am", "ig.me")),
    (Platform.FACEBOOK, ("facebook.com", "fb.watch", "fb.com", "fb.me")),
)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def extract_url(text: str | None) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(0).rstrip(").,;:!?]}'\"") if match else None


def detect_platform(url: str) -> Platform:
    host = (urlsplit(url).hostname or "").lower()
    for platform, domains in _HOST_MAP:
        if any(host == d or host.endswith("." + d) for d in domains):
            return platform
    return Platform.OTHER


# ── errors ────────────────────────────────────────────────────────────────
class DownloadError(Exception):
    """Base error; ``str(exc)`` is safe to show to the user."""


class BackendUnavailable(DownloadError):
    pass


class TaskFailed(DownloadError):
    pass


class TaskTimeout(DownloadError):
    pass


class FileTooLarge(DownloadError):
    def __init__(self, size: int):
        super().__init__(f"File is {size / 1024 / 1024:.1f} MB")
        self.size = size


# ── models ────────────────────────────────────────────────────────────────
class MediaKind(str, Enum):
    VIDEO = "video"
    AUDIO = "audio"
    IMAGE = "image"


IMAGE_EXT = (".jpeg", ".jpg", ".png", ".webp", ".gif")
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav")


def classify(name: str) -> MediaKind:
    low = (name or "").lower()
    if low.endswith(IMAGE_EXT):
        return MediaKind.IMAGE
    if low.endswith(AUDIO_EXT):
        return MediaKind.AUDIO
    return MediaKind.VIDEO


@dataclass(frozen=True)
class MediaFile:
    name: str
    url: str
    kind: MediaKind


@dataclass
class MediaResult:
    task_id: str
    platform: Platform
    source_url: str
    files: list[MediaFile] = field(default_factory=list)
    thumbnail: str | None = None

    def of(self, kind: MediaKind) -> list[MediaFile]:
        return [f for f in self.files if f.kind is kind]


@dataclass
class TaskStatus:
    state: str
    queue_position: int | None = None
    error: str | None = None
    raw: dict = field(default_factory=dict)


StatusCallback = Callable[[TaskStatus], Awaitable[None]]
ProgressCallback = Callable[[int, int | None], Awaitable[None]]


def _safe_filename(name: str, fallback: str = "download") -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name or fallback).strip("._") or fallback
    return name[:80]


# ── service ───────────────────────────────────────────────────────────────
class DownloadService:
    def __init__(self, session: aiohttp.ClientSession, base_url: str):
        self._session = session
        self.base_url = base_url.rstrip("/")

    # -- health --------------------------------------------------------
    async def online(self, timeout: float = 8) -> bool:
        try:
            async with self._session.get(f"{self.base_url}/api/server_status",
                                         timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                data = await resp.json(content_type=None)
                return bool(data.get("online"))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return False

    # -- task lifecycle ------------------------------------------------
    async def submit(self, url: str, *, retries: int = 2) -> str:
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self._session.post(
                    f"{self.base_url}/api/add", json={"url": url},
                    timeout=aiohttp.ClientTimeout(total=45),   # tolerates a cold-starting backend
                ) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status >= 400:
                        raise DownloadError(
                            str(data.get("error") or data.get("detail") or "The server rejected this link."))
                    task_id = data.get("task_id")
                    if not task_id:
                        raise DownloadError("The server returned no task id.")
                    return str(task_id)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last = exc
                log.warning("submit attempt %d failed: %r", attempt + 1, exc)
                await asyncio.sleep(2 * (attempt + 1))
        raise BackendUnavailable("The download server is not responding right now. Please try again shortly.") from last

    async def wait(self, task_id: str, on_status: StatusCallback | None = None,
                   timeout: float = 600) -> dict:
        started = time.monotonic()
        delay, max_delay, failures = 1.5, 5.0, 0
        while time.monotonic() - started < timeout:
            try:
                async with self._session.get(
                    f"{self.base_url}/api/status/{quote(task_id)}",
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status >= 400:
                        raise aiohttp.ClientError(f"HTTP {resp.status}")
                    data = await resp.json(content_type=None)
                failures = 0
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                failures += 1
                log.debug("status poll failed (%d): %r", failures, exc)
                if failures >= 8:
                    raise BackendUnavailable("Lost connection to the download server.") from exc
                delay = min(delay * 1.5, max_delay)
                await asyncio.sleep(delay)
                continue

            state = str(data.get("status") or "processing").lower()
            status = TaskStatus(state, data.get("queue_position"), data.get("error"), data)
            if on_status:
                await on_status(status)
            if state == "completed":
                return data
            if state == "failed":
                raise TaskFailed(str(data.get("error") or "Processing failed on the server."))
            delay = 2.0 if state in ("processing", "uploading") else min(delay * 1.3, max_delay)
            await asyncio.sleep(delay)
        raise TaskTimeout("The request timed out. Please try again in a few moments.")

    # -- result parsing (mirrors the web UI logic) ----------------------
    @staticmethod
    def parse_result(data: dict, platform: Platform, task_id: str, source_url: str) -> MediaResult:
        files: list[MediaFile] = []
        seen: set[str] = set()

        def add(name: str, link: str | None) -> None:
            if link and link not in seen:
                seen.add(link)
                files.append(MediaFile(name, link, classify(name)))

        add(f"{platform.value}_{task_id[:8]}.mp4", data.get("direct_link"))
        for item in data.get("files") or []:
            add(item.get("name") or "download", item.get("link"))

        thumbnail = data.get("thumbnail") or data.get("poster")
        images = [f for f in files if f.kind is MediaKind.IMAGE]
        has_media = any(f.kind is not MediaKind.IMAGE for f in files)
        if not thumbnail and len(images) == 1 and has_media:
            thumbnail = images[0].url
            files.remove(images[0])
        return MediaResult(task_id, platform, source_url, files, thumbnail)

    # -- file transfer ---------------------------------------------------
    def proxy_url(self, media: MediaFile) -> str:
        return (f"{self.base_url}/api/proxy_download?url={quote(media.url, safe='')}"
                f"&name={quote(media.name, safe='')}")

    async def fetch(self, media: MediaFile, dest_dir: Path, *, max_bytes: int,
                    on_progress: ProgressCallback | None = None) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{uuid.uuid4().hex[:8]}_{_safe_filename(media.name)}"
        timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20, sock_read=90)
        received = 0
        try:
            async with self._session.get(self.proxy_url(media), timeout=timeout) as resp:
                if resp.status != 200:
                    raise DownloadError(f"The download server returned HTTP {resp.status}.")
                length = resp.content_length
                if length and length > max_bytes:
                    raise FileTooLarge(length)
                with path.open("wb") as fh:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        received += len(chunk)
                        if received > max_bytes:
                            raise FileTooLarge(received)
                        fh.write(chunk)
                        if on_progress:
                            await on_progress(received, length)
            if received == 0:
                raise DownloadError("The server returned an empty file.")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            path.unlink(missing_ok=True)
            raise DownloadError("The file transfer was interrupted. Please try again.") from exc
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
            
