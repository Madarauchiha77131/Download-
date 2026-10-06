#!/usr/bin/env python3
"""GX Downloader — Telegram bot entry point.

Runs a long-polling bot plus a tiny HTTP server (``/health``) for Render /
UptimeRobot, in a single asyncio process.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import signal
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

import emoji
import ui
from admin import AdminPanel
from config import Config, load_config
from db import Database
from download import (DownloadError, DownloadService, FileTooLarge, MediaFile, MediaKind,
                      MediaResult, Platform, TaskStatus, detect_platform, extract_url)
from emoji import E
from tg import TelegramAPI, TelegramError

log = logging.getLogger("bot")

MAX_UPLOAD = 49 * 1024 * 1024          # Telegram Bot API upload limit is 50 MB
RESULT_TTL = 3600
JOIN_CACHE_TTL = 60
MESSAGE_EFFECT_PARTY = "5046509860389126442"


class ProgressReporter:
    """Edits one status message with an animated, throttled progress bar."""

    MIN_INTERVAL = 2.0

    def __init__(self, api: TelegramAPI, chat_id: int, message_id: int, platform: Platform):
        self.api, self.chat_id, self.message_id, self.platform = api, chat_id, message_id, platform
        self._tick = 0
        self._last = float("-inf")
        self._pct = 0
        self._creep = 0

    async def set(self, label: str, pct: int, *, force: bool = False) -> None:
        self._pct = max(self._pct, pct)
        now = time.monotonic()
        if not force and now - self._last < self.MIN_INTERVAL:
            return
        self._last = now
        self._tick += 1
        try:
            await self.api.edit_text(self.chat_id, self.message_id,
                                     ui.progress_text(self.platform, label, self._pct, self._tick))
        except TelegramError as exc:
            log.debug("progress edit failed: %s", exc)

    async def on_status(self, st: TaskStatus) -> None:
        if st.state == "waking_up":
            await self.set("Waking up server nodes", 12)
        elif st.state == "pending":
            pos = st.queue_position
            await self.set(f"In server queue #{pos}" if pos and pos > 1 else "Starting job", 20)
        elif st.state == "processing":
            self._creep = min(self._creep + 4, 30)
            await self.set("Extracting HD stream", 40 + self._creep)
        elif st.state == "uploading":
            await self.set("Packaging media", 80)
        elif st.state == "completed":
            await self.set("Media ready", 86, force=True)
        else:
            await self.set(st.state.replace("_", " ").title(), 30)

    async def on_download(self, received: int, total: int | None) -> None:
        pct = 86 + int(received / total * 10) if total else 90
        await self.set("Downloading media", pct)


class App:
    def __init__(self, cfg: Config, session: aiohttp.ClientSession, api: TelegramAPI,
                 db: Database, svc: DownloadService):
        self.cfg, self.session, self.api, self.db, self.svc = cfg, session, api, db, svc
        self.tz = ZoneInfo(cfg.timezone)
        self.started = time.time()
        self.me: dict = {}
        self.admin_ids: set[int] = set()
        self.active: set[int] = set()
        self.last_request: dict[int, float] = {}
        self.join_cache: dict[int, float] = {}
        self.results: dict[str, tuple[float, MediaResult]] = {}
        self.pending: dict[int, dict] = {}
        self.sem = asyncio.Semaphore(cfg.max_concurrent_jobs)
        self.last_poll_ok = time.time()
        self._tasks: set[asyncio.Task] = set()
        self._alert_ts = 0.0
        self.tmp_root = cfg.tmp_dir
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        self.admin = AdminPanel(self)

    # ── helpers ───────────────────────────────────────────────────────
    @property
    def channel_link(self) -> str:
        return self.db.get_setting("channel_link") or self.cfg.channel_link

    def is_owner(self, uid: int) -> bool:
        return uid == self.cfg.owner_id

    def is_admin(self, uid: int) -> bool:
        return uid == self.cfg.owner_id or uid in self.admin_ids

    def day_start(self) -> int:
        now = datetime.now(self.tz)
        return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def reload_admins(self) -> None:
        self.admin_ids = set(await self.db.list_admins())

    def apply_settings(self) -> None:
        emoji.set_premium_enabled(self.db.get_bool("premium_emoji"))

    async def alert_owner(self, text: str, cooldown: int = 3600) -> None:
        if time.time() - self._alert_ts < cooldown:
            return
        self._alert_ts = time.time()
        with contextlib.suppress(TelegramError):
            await self.api.send_message(self.cfg.owner_id, f"{E.siren} <b>Alert</b>\n{text}")

    # ── startup / loops ───────────────────────────────────────────────
    async def start(self) -> None:
        self.me = await self.api.call("getMe")
        await self.reload_admins()
        self.apply_settings()
        log.info("Bot started as @%s", self.me.get("username"))

    async def poll(self) -> None:
        await self.api.call("deleteWebhook", drop_pending_updates=False)
        offset = 0
        backoff = 1
        while True:
            try:
                updates = await self.api.call(
                    "getUpdates", offset=offset, timeout=30, _timeout=45,
                    allowed_updates=["message", "callback_query", "my_chat_member"])
                self.last_poll_ok = time.time()
                backoff = 1
            except TelegramError as exc:
                if exc.code == 409:
                    log.warning("Another instance is polling (deploy overlap?) - waiting")
                    await asyncio.sleep(5)
                else:
                    log.warning("getUpdates failed: %s", exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                self.spawn(self.handle_update(upd))

    async def maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                now = time.time()
                for key in [k for k, (ts, _) in self.results.items() if now - ts > RESULT_TTL]:
                    self.results.pop(key, None)
                for job in self.tmp_root.glob("job_*"):
                    if now - job.stat().st_mtime > 3600:
                        shutil.rmtree(job, ignore_errors=True)
                if self.db.get_bool("auto_backup") and now - self.db.get_int("last_backup") > 86400:
                    await self.admin.send_backup(self.cfg.owner_id, auto=True)
            except Exception:
                log.exception("maintenance loop error")

    # ── dispatch ──────────────────────────────────────────────────────
    async def handle_update(self, upd: dict) -> None:
        try:
            if "message" in upd:
                await self.on_message(upd["message"])
            elif "callback_query" in upd:
                await self.on_callback(upd["callback_query"])
            elif "my_chat_member" in upd:
                await self.on_my_chat_member(upd["my_chat_member"])
        except Exception:
            log.exception("update handling failed")

    async def on_my_chat_member(self, ev: dict) -> None:
        if ev["chat"]["type"] != "private":
            return
        status = ev["new_chat_member"]["status"]
        await self.db.set_blocked(ev["from"]["id"], status in ("kicked", "left"))

    # ── force join ────────────────────────────────────────────────────
    async def is_joined(self, uid: int) -> bool:
        if not self.db.get_bool("force_join") or self.is_admin(uid):
            return True
        if time.time() - self.join_cache.get(uid, 0) < JOIN_CACHE_TTL:
            return True
        try:
            channel_id = int(self.db.get_setting("channel_id") or self.cfg.channel_id)
            member = await self.api.call("getChatMember", chat_id=channel_id, user_id=uid, _timeout=15)
        except (TelegramError, ValueError) as exc:
            # Fail open so a misconfiguration never locks everyone out, but tell the owner.
            log.error("Force-join check failed: %s", exc)
            await self.alert_owner(
                f"Force-join check failed:\n<code>{ui.esc(exc)}</code>\n"
                f"Make sure the bot is an <b>admin</b> of the channel.")
            return True
        status = member.get("status")
        joined = status in ("creator", "administrator", "member") or (
            status == "restricted" and member.get("is_member"))
        if joined:
            self.join_cache[uid] = time.time()
        return bool(joined)

    async def send_join_prompt(self, chat_id: int) -> None:
        await self.api.send_message(chat_id, ui.join_text(), ui.join_keyboard(self.channel_link))

    # ── messages ──────────────────────────────────────────────────────
    async def on_message(self, msg: dict) -> None:
        if msg["chat"]["type"] != "private" or "from" not in msg or msg["from"].get("is_bot"):
            return
        user = msg["from"]
        uid, chat_id = user["id"], msg["chat"]["id"]
        is_new = await self.db.upsert_user(uid, user.get("username"), user.get("first_name"), user.get("last_name"))
        if is_new and self.db.get_bool("notify_new_users"):
            with contextlib.suppress(TelegramError):
                await self.api.send_message(
                    self.cfg.owner_id,
                    f"{E.new} New user: <a href=\"tg://user?id={uid}\">{ui.esc(user.get('first_name'))}</a> "
                    f"<code>{uid}</code>")

        record = await self.db.get_user(uid)
        if record and record["is_banned"] and not self.is_admin(uid):
            await self.api.send_message(chat_id, ui.banned_text(record.get("ban_reason") or ""))
            return

        text = (msg.get("text") or msg.get("caption") or "").strip()
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

        if self.is_admin(uid):
            if command == "/cancel" and self.pending.pop(uid, None):
                await self.api.send_message(chat_id, f"{E.ok} Cancelled.")
                return
            if command == "/start":
                self.pending.pop(uid, None)
            elif uid in self.pending and command != "/admin":
                await self.admin.on_input(msg)
                return
            if command == "/admin":
                self.pending.pop(uid, None)
                await self.admin.open((chat_id, None))
                return

        if self.db.get_bool("maintenance") and not self.is_admin(uid):
            await self.api.send_message(chat_id, ui.maintenance_text(self.db.get_setting("maintenance_msg")))
            return

        if command == "/id":
            await self.api.send_message(chat_id, f"{E.user} <b>Your ID:</b> <code>{uid}</code>")
            return

        if not await self.is_joined(uid):
            await self.send_join_prompt(chat_id)
            return

        if command in ("/start", "/menu"):
            await self.send_welcome(chat_id, user, animate=True)
        elif command == "/help":
            await self.api.send_message(chat_id, ui.guide_text(), ui.back_menu())
        elif command == "/ping":
            await self.api.send_message(chat_id, f"{E.green} <b>Online</b> • up {ui.fmt_duration(time.time() - self.started)}")
        elif command.startswith("/"):
            await self.api.send_message(chat_id, ui.invalid_link_text(),
                                        ui.main_menu(self.is_admin(uid), self.channel_link, self.cfg.owner_id))
        else:
            await self.handle_link(chat_id, user, text)

    async def send_welcome(self, chat_id: int, user: dict, animate: bool = False) -> None:
        if animate:
            try:
                frame = await self.api.send_message(chat_id, ui.boot_frame(0, "Initializing"))
                for pct, label in ((35, "Connecting servers"), (70, "Loading modules"), (100, "Ready")):
                    await asyncio.sleep(0.45)
                    await self.api.edit_text(chat_id, frame["message_id"], ui.boot_frame(pct, label))
                await asyncio.sleep(0.35)
                await self.api.delete(chat_id, frame["message_id"])
            except TelegramError as exc:
                log.debug("boot animation failed: %s", exc)
        text = ui.welcome_text(user.get("first_name") or "friend", self.cfg.brand, self.db.get_setting("welcome"))
        markup = ui.main_menu(self.is_admin(user["id"]), self.channel_link, self.cfg.owner_id)
        try:
            await self.api.send_message(chat_id, text, markup, message_effect_id=MESSAGE_EFFECT_PARTY)
        except TelegramError:
            await self.api.send_message(chat_id, text, markup)

    # ── download flow ─────────────────────────────────────────────────
    async def handle_link(self, chat_id: int, user: dict, text: str) -> None:
        uid = user["id"]
        url = extract_url(text)
        if not url:
            await self.api.send_message(chat_id, ui.invalid_link_text(),
                                        ui.main_menu(self.is_admin(uid), self.channel_link, self.cfg.owner_id))
            return
        platform = detect_platform(url)
        if not self.db.get_bool(f"platform_{platform.value}") and not self.is_admin(uid):
            await self.api.send_message(chat_id, ui.platform_disabled_text(platform))
            return
        if uid in self.active:
            await self.api.send_message(chat_id, ui.busy_text())
            return
        if not self.is_admin(uid):
            cooldown = self.db.get_int("cooldown")
            wait = cooldown - (time.time() - self.last_request.get(uid, 0))
            if wait > 0:
                await self.api.send_message(chat_id, ui.cooldown_text(int(wait) + 1))
                return
            limit = self.db.get_int("daily_limit")
            if limit > 0 and await self.db.count_ok_since(uid, self.day_start()) >= limit:
                await self.api.send_message(chat_id, ui.limit_text(limit))
                return
        self.active.add(uid)
        self.last_request[uid] = time.time()
        try:
            await self.run_download(chat_id, uid, url, platform)
        finally:
            self.active.discard(uid)

    async def run_download(self, chat_id: int, uid: int, url: str, platform: Platform) -> None:
        started = time.monotonic()
        status = await self.api.send_message(chat_id, ui.progress_text(platform, "Connecting", 5, 0))
        reporter = ProgressReporter(self.api, chat_id, status["message_id"], platform)
        task_id = ""
        try:
            if self.sem.locked():
                await reporter.set("Waiting in bot queue", 8, force=True)
            async with self.sem:
                await reporter.set("Dispatching to pipeline", 10, force=True)
                task_id = await self.svc.submit(url)
                raw = await self.svc.wait(task_id, reporter.on_status)
                result = self.svc.parse_result(raw, platform, task_id, url)
                if not result.files:
                    raise DownloadError("The server processed the link but found no downloadable media.")
                size = await self.deliver(chat_id, result, reporter)
            await self.api.delete(chat_id, status["message_id"])
            await self.db.record_download(uid, url, platform.value, True, "", task_id, size,
                                          int((time.monotonic() - started) * 1000))
        except DownloadError as exc:
            await self._fail(chat_id, status["message_id"], uid, url, platform, task_id, str(exc), started)
        except TelegramError as exc:
            log.error("Telegram error during delivery: %s", exc)
            await self._fail(chat_id, status["message_id"], uid, url, platform, task_id,
                             "Telegram refused the file. Please try again.", started, detail=str(exc))
        except Exception as exc:
            log.exception("download crashed")
            await self._fail(chat_id, status["message_id"], uid, url, platform, task_id,
                             "Something went wrong. Please try again.", started, detail=repr(exc))

    async def _fail(self, chat_id: int, message_id: int, uid: int, url: str, platform: Platform,
                    task_id: str, message: str, started: float, detail: str = "") -> None:
        await self.db.record_download(uid, url, platform.value, False, detail or message, task_id, 0,
                                      int((time.monotonic() - started) * 1000))
        with contextlib.suppress(TelegramError):
            await self.api.edit_text(chat_id, message_id, ui.error_text(message))

    # ── delivery ──────────────────────────────────────────────────────
    def _remember(self, result: MediaResult) -> str:
        key = result.task_id[:20].replace(":", "")
        self.results[key] = (time.time(), result)
        return key

    async def deliver(self, chat_id: int, result: MediaResult, reporter: ProgressReporter) -> int:
        workdir = Path(tempfile.mkdtemp(prefix="job_", dir=self.tmp_root))
        total = 0
        try:
            videos, audios, images = (result.of(MediaKind.VIDEO), result.of(MediaKind.AUDIO),
                                      result.of(MediaKind.IMAGE))
            cap = ui.caption(result.platform, self.me.get("username", "bot"))
            markup = ui.result_keyboard(self._remember(result) if (audios and videos) else None, result.source_url)
            primary = videos or ([] if images else audios)
            for i, media in enumerate(primary):
                last = i == len(primary) - 1
                total += await self._send_file(chat_id, media, workdir, cap if i == 0 else None,
                                               markup if last else None, reporter)
            if images:
                total += await self._send_images(chat_id, images, workdir, None if primary else cap, reporter)
            return total
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    async def _send_file(self, chat_id: int, media: MediaFile, workdir: Path, caption: str | None,
                         markup: dict | None, reporter: ProgressReporter | None) -> int:
        try:
            path = await self.svc.fetch(media, workdir, max_bytes=MAX_UPLOAD,
                                        on_progress=reporter.on_download if reporter else None)
        except FileTooLarge as exc:
            link = self.svc.proxy_url(media)
            extra = ui.kb([ui.btn("Download", url=link, icon="download", style="success")]) if len(link) <= 2000 else None
            await self.api.send_message(chat_id, ui.too_large_text(exc.size, link), extra)
            return 0
        method, field, action, extra_params = {
            MediaKind.VIDEO: ("sendVideo", "video", "upload_video", {"supports_streaming": True}),
            MediaKind.AUDIO: ("sendAudio", "audio", "upload_voice", {}),
            MediaKind.IMAGE: ("sendPhoto", "photo", "upload_photo", {}),
        }[media.kind]
        await self.api.action(chat_id, action)
        common = dict(chat_id=chat_id, caption=caption, parse_mode="HTML", reply_markup=markup)
        try:
            await self.api.upload(method, {field: path}, **common, **extra_params)
        except TelegramError as exc:
            if exc.code != 400:
                raise
            log.info("%s rejected (%s) - falling back to sendDocument", method, exc.description)
            await self.api.upload("sendDocument", {"document": path}, **common)
        return path.stat().st_size

    async def _send_images(self, chat_id: int, images: list[MediaFile], workdir: Path,
                           caption: str | None, reporter: ProgressReporter | None) -> int:
        paths: list[Path] = []
        for media in images[:30]:
            try:
                paths.append(await self.svc.fetch(media, workdir, max_bytes=MAX_UPLOAD))
            except DownloadError as exc:
                log.warning("image skipped: %s", exc)
        if not paths:
            raise DownloadError("Could not download the images.")
        await self.api.action(chat_id, "upload_photo")
        total = 0
        for start in range(0, len(paths), 10):
            chunk = paths[start:start + 10]
            cap = caption if start == 0 else None
            total += sum(p.stat().st_size for p in chunk)
            if len(chunk) == 1:
                try:
                    await self.api.upload("sendPhoto", {"photo": chunk[0]}, chat_id=chat_id,
                                          caption=cap, parse_mode="HTML")
                except TelegramError as exc:
                    if exc.code != 400:
                        raise
                    await self.api.upload("sendDocument", {"document": chunk[0]}, chat_id=chat_id,
                                          caption=cap, parse_mode="HTML")
                continue
            for kind, field_type in (("photo", "photo"), ("document", "document")):
                files = {f"f{i}": p for i, p in enumerate(chunk)}
                items = [{"type": field_type, "media": f"attach://f{i}"} for i in range(len(chunk))]
                if cap:
                    items[0].update(caption=cap, parse_mode="HTML")
                try:
                    await self.api.upload("sendMediaGroup", files, chat_id=chat_id, media=items)
                    break
                except TelegramError as exc:
                    if exc.code != 400 or kind == "document":
                        raise
                    log.info("photo album rejected (%s) - sending as documents", exc.description)
        return total

    # ── callbacks ─────────────────────────────────────────────────────
    async def on_callback(self, cb: dict) -> None:
        user, data = cb["from"], cb.get("data") or ""
        uid, cid = user["id"], cb["id"]
        msg = cb.get("message") or {}
        chat_id, message_id = msg.get("chat", {}).get("id"), msg.get("message_id")
        if not chat_id:
            await self.api.answer(cid)
            return
        record = await self.db.get_user(uid)
        if record and record["is_banned"] and not self.is_admin(uid):
            await self.api.answer(cid, "You are banned.", True)
            return

        if data.startswith("adm:"):
            if not self.is_admin(uid):
                await self.api.answer(cid, "Admins only.", True)
                return
            await self.admin.on_callback(cb)
            return

        if self.db.get_bool("maintenance") and not self.is_admin(uid):
            await self.api.answer(cid, "The bot is under maintenance.", True)
            return

        if data == "check_join":
            self.join_cache.pop(uid, None)
            if await self.is_joined(uid):
                await self.api.answer(cid, "Verified ✅")
                await self.api.delete(chat_id, message_id)
                await self.send_welcome(chat_id, user, animate=False)
            else:
                await self.api.answer(cid, "You haven't joined yet. Join the channel, then tap the button again.", True)
            return

        if not await self.is_joined(uid):
            await self.api.answer(cid, "Please join the channel first.", True)
            await self.send_join_prompt(chat_id)
            return

        if data.startswith("menu:"):
            await self.api.answer(cid)
            await self._show_menu(chat_id, message_id, user, data.split(":", 1)[1])
        elif data.startswith("aud:"):
            await self._send_audio(cb, chat_id, data[4:])
        else:
            await self.api.answer(cid)

    async def _show_menu(self, chat_id: int, message_id: int, user: dict, page: str) -> None:
        uid = user["id"]
        markup: dict | None = ui.back_menu()
        if page == "guide":
            text = ui.guide_text()
        elif page == "platforms":
            text = ui.platforms_text()
        elif page == "faq":
            text = ui.faq_text()
        elif page == "profile":
            record = await self.db.get_user(uid) or {"user_id": uid, "username": user.get("username"),
                                                      "first_name": user.get("first_name"),
                                                      "joined_at": int(time.time()), "downloads": 0}
            used = await self.db.count_ok_since(uid, self.day_start())
            text = ui.profile_text(record, self.cfg.timezone, used, self.db.get_int("daily_limit"), self.is_admin(uid))
        else:
            text = ui.welcome_text(user.get("first_name") or "friend", self.cfg.brand, self.db.get_setting("welcome"))
            markup = ui.main_menu(self.is_admin(uid), self.channel_link, self.cfg.owner_id)
        try:
            await self.api.edit_text(chat_id, message_id, text, markup)
        except TelegramError as exc:
            log.debug("menu edit failed: %s", exc)

    async def _send_audio(self, cb: dict, chat_id: int, key: str) -> None:
        entry = self.results.get(key)
        if not entry:
            await self.api.answer(cb["id"], "This request expired. Please send the link again.", True)
            return
        await self.api.answer(cb["id"], "Preparing audio…")
        audios = entry[1].of(MediaKind.AUDIO)
        if not audios:
            return
        workdir = Path(tempfile.mkdtemp(prefix="job_", dir=self.tmp_root))
        try:
            cap = ui.caption(entry[1].platform, self.me.get("username", "bot"))
            await self._send_file(chat_id, audios[0], workdir, cap, None, None)
        except (DownloadError, TelegramError) as exc:
            log.warning("audio send failed: %s", exc)
            await self.api.send_message(chat_id, ui.error_text("Could not send the audio file."))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# ── HTTP health endpoint (UptimeRobot / Render) ───────────────────────────
async def start_health_server(app: App, port: int):
    from aiohttp import web

    async def health(request: web.Request) -> web.Response:
        polling_age = time.time() - app.last_poll_ok
        payload = {
            "status": "ok",
            "bot": app.me.get("username"),
            "uptime_s": int(time.time() - app.started),
            "active_jobs": len(app.active),
            "polling_age_s": int(polling_age),
        }
        code = 200
        if polling_age > 300:
            payload["status"], code = "degraded", 503
        if request.query.get("deep") == "1":
            try:
                await app.api.call("getMe", _timeout=8)
                payload["db"] = await app.db.ping()
                payload["backend_online"] = await app.svc.online(timeout=6)
            except Exception as exc:
                payload["status"], payload["error"], code = "degraded", repr(exc), 503
        return web.json_response(payload, status=code)

    web_app = web.Application()
    for route in ("/", "/health", "/healthz"):
        web_app.router.add_get(route, health)
    runner = web.AppRunner(web_app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Health server listening on :%d (/health)", port)
    return runner


async def amain() -> None:
    cfg = load_config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)

    connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector,
                                     headers={"User-Agent": "GX-Downloader-Bot/1.0"}) as session:
        api = TelegramAPI(cfg.bot_token, session)
        db = Database(cfg.db_path)
        await db.init({"channel_id": str(cfg.channel_id), "channel_link": cfg.channel_link})
        app = App(cfg, session, api, db, DownloadService(session, cfg.backend_url))
        await app.start()
        runner = await start_health_server(app, cfg.port)

        tasks = [asyncio.create_task(app.poll()), asyncio.create_task(app.maintenance_loop())]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        log.info("Shutting down…")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        db.close()


if __name__ == "__main__":
    asyncio.run(amain())
