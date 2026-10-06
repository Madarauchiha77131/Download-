"""Admin panel: statistics, broadcast, user management, settings, backup and more.

Callback data format: ``adm:<section>[:<arg>...]`` -> ``cb_<section>`` handler.
Text input flows use ``App.pending[user_id] = {"action": ...}`` -> ``in_<action>``.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import resource
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import emoji
import ui
from db import Database
from download import Platform
from emoji import E
from tg import TelegramError

if TYPE_CHECKING:
    from bot import App

log = logging.getLogger("admin")

OWNER_ONLY = {"ad", "rs"}
PLATFORMS = ("youtube", "tiktok", "instagram", "facebook", "other")
TOGGLES = {
    "premium_emoji": "Premium emoji",
    "auto_backup": "Daily auto-backup to owner",
    "notify_new_users": "New-user alerts to owner",
}


def _onoff(flag: bool) -> str:
    return f"{E.green} ON" if flag else f"{E.red} OFF"


class AdminPanel:
    def __init__(self, app: "App"):
        self.app = app
        self.bc_task: asyncio.Task | None = None
        self.bc_stop = False

    # shortcuts
    @property
    def api(self):
        return self.app.api

    @property
    def db(self) -> Database:
        return self.app.db

    # ── plumbing ──────────────────────────────────────────────────────
    async def _show(self, ctx: tuple[int, int | None], text: str, markup: dict | None = None) -> None:
        chat_id, message_id = ctx
        if message_id:
            try:
                await self.api.edit_text(chat_id, message_id, text, markup)
                return
            except TelegramError as exc:
                log.debug("admin edit failed (%s) - sending new message", exc)
        await self.api.send_message(chat_id, text, markup)

    @staticmethod
    def _back(target: str = "adm:home") -> list[dict]:
        return [ui.btn("Back", cb=target, icon="back", style="primary")]

    async def _prompt(self, ctx, uid: int, action: str, text: str, **extra) -> None:
        self.app.pending[uid] = {"action": action, **extra}
        await self._show(ctx, f"{text}\n\n<i>Send /cancel to abort.</i>",
                         ui.kb([ui.btn("Cancel", cb="adm:cancel", icon="no", style="danger")]))

    async def on_callback(self, cb: dict) -> None:
        uid, msg = cb["from"]["id"], cb["message"]
        ctx = (msg["chat"]["id"], msg["message_id"])
        parts = cb["data"].split(":")
        section, args = (parts[1] if len(parts) > 1 else "home"), parts[2:]
        handler = getattr(self, f"cb_{section}", None)
        if handler is None:
            await self.api.answer(cb["id"])
            return
        if section in OWNER_ONLY and not self.app.is_owner(uid):
            await self.api.answer(cb["id"], "Owner only.", True)
            return
        toast = None
        try:
            toast = await handler(cb, ctx, args)
        except TelegramError as exc:
            log.warning("admin handler %s failed: %s", section, exc)
            toast = f"Error: {exc.description[:150]}"
        except Exception:
            log.exception("admin handler %s crashed", section)
            toast = "Unexpected error - see logs."
        await self.api.answer(cb["id"], toast if isinstance(toast, str) else None)

    async def on_input(self, msg: dict) -> None:
        uid = msg["from"]["id"]
        state = self.app.pending.get(uid)
        if not state:
            return
        handler = getattr(self, f"in_{state['action']}", None)
        if handler is None:
            self.app.pending.pop(uid, None)
            return
        try:
            await handler(msg, state)
        except TelegramError as exc:
            await self.api.send_message(msg["chat"]["id"], f"{E.no} Telegram error: <code>{ui.esc(exc.description)}</code>")
        except Exception:
            log.exception("admin input %s crashed", state["action"])
            self.app.pending.pop(uid, None)
            await self.api.send_message(msg["chat"]["id"], f"{E.no} Unexpected error - see logs.")

    # ── home ──────────────────────────────────────────────────────────
    async def open(self, ctx) -> None:
        text = (f"{E.crown} <b>Admin Panel</b>\n{ui.DIV}\n"
                f"<blockquote>Manage users, broadcasts, limits, security and backups.</blockquote>")
        b = ui.btn
        markup = ui.kb(
            [b("Statistics", cb="adm:stats", icon="chart", style="primary"),
             b("Broadcast", cb="adm:bc", icon="megaphone", style="success")],
            [b("Users", cb="adm:users", icon="users"), b("Find User", cb="adm:find", icon="search")],
            [b("Ban User", cb="adm:ban", icon="stop", style="danger"),
             b("Unban User", cb="adm:unban", icon="ok", style="success")],
            [b("Force Join", cb="adm:fj", icon="shield"), b("Maintenance", cb="adm:mt", icon="wrench")],
            [b("Limits", cb="adm:lim", icon="alarm"), b("Platforms", cb="adm:pf", icon="globe")],
            [b("Welcome Text", cb="adm:wl", icon="pin"), b("Logs", cb="adm:lg", icon="info")],
            [b("Backup", cb="adm:bk", icon="backup"), b("Restore", cb="adm:rs", icon="upload")],
            [b("Health", cb="adm:hl", icon="green"), b("Emoji Test", cb="adm:em", icon="sparkles")],
            [b("Admins", cb="adm:ad", icon="crown"), b("Settings", cb="adm:st", icon="gear")],
            [b("Close", cb="adm:close", icon="no", style="danger")],
        )
        await self._show(ctx, text, markup)

    async def cb_home(self, cb, ctx, args):
        self.app.pending.pop(cb["from"]["id"], None)
        await self.open(ctx)

    async def cb_cancel(self, cb, ctx, args):
        self.app.pending.pop(cb["from"]["id"], None)
        await self.open(ctx)
        return "Cancelled"

    async def cb_close(self, cb, ctx, args):
        await self.api.delete(*ctx)

    # ── statistics ────────────────────────────────────────────────────
    async def cb_stats(self, cb, ctx, args):
        s = await self.db.stats(self.app.day_start())
        total = s["dl_ok"] + s["dl_fail"]
        rate = f"{s['dl_ok'] / total * 100:.0f}%" if total else "—"
        platforms = " · ".join(f"{p.title()} <b>{n}</b>" for p, n in s["platforms"].items()) or "—"
        text = (
            f"{E.chart} <b>Statistics</b>\n{ui.DIV}\n"
            f"{E.users} <b>Users:</b> {s['users']}  (+{s['new_today']} today)\n"
            f"{E.green} <b>Active:</b> {s['active_24h']} (24h) · {s['active_7d']} (7d)\n"
            f"{E.stop} <b>Banned:</b> {s['banned']} · <b>Blocked bot:</b> {s['blocked']}\n"
            f"{ui.DIV}\n"
            f"{E.download} <b>Downloads:</b> {s['dl_ok']} ok · {s['dl_fail']} failed ({rate})\n"
            f"{E.calendar} <b>Today:</b> {s['dl_today']}\n"
            f"{E.bolt} <b>Avg time:</b> {s['avg_ms'] / 1000:.1f}s\n"
            f"{E.backup} <b>Data sent:</b> {ui.fmt_size(s['bytes'])}\n"
            f"{ui.DIV}\n{E.globe} {platforms}\n"
            f"{E.clock} <b>Uptime:</b> {ui.fmt_duration(time.time() - self.app.started)}"
        )
        await self._show(ctx, text, ui.kb(
            [ui.btn("Refresh", cb="adm:stats", icon="refresh", style="success")], self._back()))

    # ── broadcast ─────────────────────────────────────────────────────
    async def cb_bc(self, cb, ctx, args):
        uid = cb["from"]["id"]
        if not args:
            if self.bc_task and not self.bc_task.done():
                return "A broadcast is already running."
            await self._prompt(ctx, uid, "bc",
                               f"{E.megaphone} <b>Broadcast</b>\n{ui.DIV}\n"
                               f"Send the message to broadcast — text, photo, video, document… "
                               f"formatting and premium emoji are preserved.")
            return None
        if args[0] == "stop":
            self.bc_stop = True
            return "Stopping…"
        state = self.app.pending.get(uid)
        if args[0] == "go" and state and state["action"] == "bc_confirm":
            if self.bc_task and not self.bc_task.done():
                return "A broadcast is already running."
            self.app.pending.pop(uid, None)
            audience = args[1] if len(args) > 1 else "all"
            self.bc_task = self.app.spawn(self._run_broadcast(ctx[0], state["src_chat"], state["src_msg"], audience))
            return "Broadcast started"
        return "Nothing to send - start again."

    async def in_bc(self, msg: dict, state: dict) -> None:
        uid, chat_id = msg["from"]["id"], msg["chat"]["id"]
        self.app.pending[uid] = {"action": "bc_confirm", "src_chat": chat_id, "src_msg": msg["message_id"]}
        everyone = len(await self.db.broadcast_targets())
        recent = len(await self.db.broadcast_targets(active_days=7))
        await self.api.call("copyMessage", chat_id=chat_id, from_chat_id=chat_id, message_id=msg["message_id"])
        await self.api.send_message(
            chat_id,
            f"{E.eyes} <b>Preview above.</b> Choose the audience:\n"
            f"<blockquote>All: <b>{everyone}</b> · Active 7d: <b>{recent}</b></blockquote>\n"
            f"<i>Send another message to replace the preview.</i>",
            ui.kb([ui.btn(f"Send to All ({everyone})", cb="adm:bc:go:all", icon="rocket", style="success"),
                   ui.btn(f"Active 7d ({recent})", cb="adm:bc:go:7d", icon="bolt", style="primary")],
                  [ui.btn("Cancel", cb="adm:cancel", icon="no", style="danger")]))

    in_bc_confirm = in_bc

    async def _copy_one(self, uid: int, src_chat: int, src_msg: int) -> str:
        try:
            await self.api.call("copyMessage", chat_id=uid, from_chat_id=src_chat, message_id=src_msg)
            return "ok"
        except TelegramError as exc:
            if exc.blocked or "deactivated" in exc.description or "chat not found" in exc.description:
                await self.db.set_blocked(uid, True)
                return "blocked"
            return "fail"

    async def _run_broadcast(self, admin_chat: int, src_chat: int, src_msg: int, audience: str) -> None:
        ids = await self.db.broadcast_targets(active_days=7 if audience == "7d" else None)
        total, sent, failed, blocked = len(ids), 0, 0, 0
        self.bc_stop = False
        stop_kb = ui.kb([ui.btn("Stop", cb="adm:bc:stop", icon="stop", style="danger")])

        def render(done: bool = False, stopped: bool = False) -> str:
            pct = int((sent + failed + blocked) / total * 100) if total else 100
            title = "Broadcast stopped" if stopped else ("Broadcast finished" if done else "Broadcasting…")
            return (f"{E.megaphone} <b>{title}</b>\n{ui.DIV}\n<code>{ui.bar(pct)} {pct}%</code>\n"
                    f"{E.ok} Sent: <b>{sent}</b>\n{E.stop} Blocked: <b>{blocked}</b>\n{E.no} Failed: <b>{failed}</b>\n"
                    f"{E.users} Total: <b>{total}</b>")

        status = await self.api.send_message(admin_chat, render(), stop_kb)
        last_edit = 0.0
        for start in range(0, total, 20):
            if self.bc_stop:
                break
            batch = ids[start:start + 20]
            results = await asyncio.gather(*(self._copy_one(u, src_chat, src_msg) for u in batch))
            sent += results.count("ok")
            blocked += results.count("blocked")
            failed += results.count("fail")
            if time.monotonic() - last_edit > 3:
                last_edit = time.monotonic()
                with contextlib.suppress(TelegramError):
                    await self.api.edit_text(admin_chat, status["message_id"], render(), stop_kb)
            await asyncio.sleep(1.0)       # stay well under Telegram's ~30 msg/s limit
        with contextlib.suppress(TelegramError):
            await self.api.edit_text(admin_chat, status["message_id"], render(True, self.bc_stop),
                                     ui.kb(self._back()))

    # ── users ─────────────────────────────────────────────────────────
    async def cb_users(self, cb, ctx, args):
        if not args:
            s = await self.db.stats(self.app.day_start())
            text = (f"{E.users} <b>Users</b>\n{ui.DIV}\n"
                    f"Total: <b>{s['users']}</b> · Active 7d: <b>{s['active_7d']}</b>\n"
                    f"Banned: <b>{s['banned']}</b> · Blocked bot: <b>{s['blocked']}</b>")
            await self._show(ctx, text, ui.kb(
                [ui.btn("Export CSV", cb="adm:users:csv", icon="file", style="success"),
                 ui.btn("Top Users", cb="adm:users:top", icon="trophy", style="primary")],
                [ui.btn("Banned List", cb="adm:users:banned", icon="stop")], self._back()))
            return None
        chat_id = ctx[0]
        if args[0] == "csv":
            data = await self.db.export_users_csv()
            path = self.app.tmp_root / f"users_{datetime.now(self.app.tz):%Y%m%d_%H%M}.csv"
            path.write_text(data, encoding="utf-8")
            try:
                await self.api.upload("sendDocument", {"document": path}, chat_id=chat_id,
                                      caption=f"{E.users} Users export", parse_mode="HTML")
            finally:
                path.unlink(missing_ok=True)
            return "Exported"
        if args[0] == "top":
            rows = await self.db.top_users(10)
            lines = [f"{i}. {ui.esc(u['first_name'] or u['user_id'])} "
                     f"<code>{u['user_id']}</code> — <b>{u['downloads']}</b>" for i, u in enumerate(rows, 1)]
            await self._show(ctx, f"{E.trophy} <b>Top Users</b>\n{ui.DIV}\n" + ("\n".join(lines) or "No downloads yet."),
                             ui.kb(self._back("adm:users")))
        elif args[0] == "banned":
            rows = await self.db.banned_users()
            lines = [f"{E.stop} {ui.esc(u['first_name'] or '')} <code>{u['user_id']}</code>"
                     + (f" — {ui.esc(u['ban_reason'])}" if u["ban_reason"] else "") for u in rows]
            await self._show(ctx, f"{E.stop} <b>Banned Users</b>\n{ui.DIV}\n" + ("\n".join(lines) or "Nobody is banned."),
                             ui.kb(self._back("adm:users")))
        return None

    async def cb_find(self, cb, ctx, args):
        await self._prompt(ctx, cb["from"]["id"], "find",
                           f"{E.search} <b>Find User</b>\n{ui.DIV}\nSend a user <b>ID</b> or <b>@username</b>.")

    async def in_find(self, msg: dict, state: dict) -> None:
        self.app.pending.pop(msg["from"]["id"], None)
        user = await self.db.find_user(msg.get("text") or "")
        chat_id = msg["chat"]["id"]
        if not user:
            await self.api.send_message(chat_id, f"{E.no} User not found. They must have started the bot.",
                                        ui.kb(self._back()))
            return
        await self._user_card((chat_id, None), user)

    async def _user_card(self, ctx, user: dict) -> None:
        tz = self.app.cfg.timezone
        uname = f"@{ui.esc(user['username'])}" if user.get("username") else "—"
        status = f"{E.stop} Banned" if user["is_banned"] else f"{E.green} Active"
        if user["is_blocked"]:
            status += " · blocked the bot"
        text = (f"{E.user} <b>{ui.esc(user.get('first_name') or 'User')}</b> {uname}\n{ui.DIV}\n"
                f"<b>ID:</b> <code>{user['user_id']}</code>\n<b>Status:</b> {status}\n"
                f"<b>Joined:</b> {ui.fmt_ts(user['joined_at'], tz)}\n"
                f"<b>Last seen:</b> {ui.fmt_ts(user['last_seen'], tz)}\n"
                f"<b>Downloads:</b> {user['downloads']}")
        if user["is_banned"] and user.get("ban_reason"):
            text += f"\n<b>Reason:</b> {ui.esc(user['ban_reason'])}"
        toggle = (ui.btn("Unban", cb=f"adm:u:unban:{user['user_id']}", icon="ok", style="success")
                  if user["is_banned"] else
                  ui.btn("Ban", cb=f"adm:u:ban:{user['user_id']}", icon="stop", style="danger"))
        await self._show(ctx, text, ui.kb(
            [toggle, ui.btn("Message", cb=f"adm:u:msg:{user['user_id']}", icon="mail", style="primary")],
            self._back()))

    async def cb_u(self, cb, ctx, args):
        action, target = args[0], int(args[1])
        if action in ("ban", "unban"):
            if self.app.is_admin(target):
                return "You can't ban an admin."
            await self.db.set_banned(target, action == "ban", "Banned by admin")
            await self._user_card(ctx, await self.db.get_user(target))
            return "Done"
        if action == "msg":
            await self._prompt(ctx, cb["from"]["id"], "dm",
                               f"{E.mail} <b>Message user</b> <code>{target}</code>\nSend what you want to deliver.",
                               target=target)
        return None

    async def in_dm(self, msg: dict, state: dict) -> None:
        self.app.pending.pop(msg["from"]["id"], None)
        chat_id = msg["chat"]["id"]
        try:
            await self.api.call("copyMessage", chat_id=state["target"], from_chat_id=chat_id,
                                message_id=msg["message_id"])
            await self.api.send_message(chat_id, f"{E.ok} Delivered.", ui.kb(self._back()))
        except TelegramError as exc:
            await self.api.send_message(chat_id, f"{E.no} Could not deliver: <code>{ui.esc(exc.description)}</code>",
                                        ui.kb(self._back()))

    async def cb_ban(self, cb, ctx, args):
        await self._prompt(ctx, cb["from"]["id"], "ban",
                           f"{E.stop} <b>Ban User</b>\n{ui.DIV}\nSend: <code>user_id reason (optional)</code>")

    async def in_ban(self, msg: dict, state: dict) -> None:
        self.app.pending.pop(msg["from"]["id"], None)
        chat_id = msg["chat"]["id"]
        parts = (msg.get("text") or "").split(maxsplit=1)
        if not parts or not parts[0].lstrip("-").isdigit():
            await self.api.send_message(chat_id, f"{E.no} Send a numeric user ID.", ui.kb(self._back()))
            return
        target = int(parts[0])
        if self.app.is_admin(target):
            await self.api.send_message(chat_id, f"{E.no} You can't ban an admin.", ui.kb(self._back()))
            return
        ok = await self.db.set_banned(target, True, parts[1] if len(parts) > 1 else "")
        await self.api.send_message(
            chat_id, f"{E.ok} <code>{target}</code> banned." if ok else f"{E.no} User not found in the database.",
            ui.kb(self._back()))

    async def cb_unban(self, cb, ctx, args):
        await self._prompt(ctx, cb["from"]["id"], "unban",
                           f"{E.ok} <b>Unban User</b>\n{ui.DIV}\nSend the <b>user ID</b>.")

    async def in_unban(self, msg: dict, state: dict) -> None:
        self.app.pending.pop(msg["from"]["id"], None)
        chat_id, text = msg["chat"]["id"], (msg.get("text") or "").strip()
        ok = text.lstrip("-").isdigit() and await self.db.set_banned(int(text), False)
        await self.api.send_message(
            chat_id, f"{E.ok} <code>{text}</code> unbanned." if ok else f"{E.no} User not found.",
            ui.kb(self._back()))

    # ── force join ────────────────────────────────────────────────────
    async def cb_fj(self, cb, ctx, args):
        uid = cb["from"]["id"]
        if args and args[0] == "t":
            await self.db.set_setting("force_join", "0" if self.db.get_bool("force_join") else "1")
            self.app.join_cache.clear()
        elif args and args[0] == "set":
            await self._prompt(ctx, uid, "fj_channel",
                               f"{E.shield} <b>Change Channel</b>\n{ui.DIV}\nSend: <code>channel_id invite_link</code>\n"
                               f"Example: <code>-1001234567890 https://t.me/+AbCdEf</code>\n"
                               f"<i>The bot must be an admin of that channel.</i>")
            return None
        elif args and args[0] == "test":
            return await self._fj_test(ctx)
        text = (f"{E.shield} <b>Force Join</b>\n{ui.DIV}\n"
                f"<b>Status:</b> {_onoff(self.db.get_bool('force_join'))}\n"
                f"<b>Channel ID:</b> <code>{ui.esc(self.db.get_setting('channel_id'))}</code>\n"
                f"<b>Link:</b> {ui.esc(self.app.channel_link)}")
        await self._show(ctx, text, ui.kb(
            [ui.btn("Toggle", cb="adm:fj:t", icon="refresh", style="primary"),
             ui.btn("Test", cb="adm:fj:test", icon="green", style="success")],
            [ui.btn("Change Channel", cb="adm:fj:set", icon="pin")], self._back()))
        return None

    async def _fj_test(self, ctx) -> str:
        try:
            channel_id = int(self.db.get_setting("channel_id"))
            chat = await self.api.call("getChat", chat_id=channel_id)
            me = await self.api.call("getChatMember", chat_id=channel_id, user_id=self.app.me["id"])
            count = await self.api.call("getChatMemberCount", chat_id=channel_id)
            is_admin = me.get("status") in ("administrator", "creator")
            report = (f"{E.shield} <b>Channel Test</b>\n{ui.DIV}\n<b>Title:</b> {ui.esc(chat.get('title'))}\n"
                      f"<b>Members:</b> {count}\n<b>Bot status:</b> {ui.esc(me.get('status'))} "
                      f"{E.ok if is_admin else E.warn}\n")
            if not is_admin:
                report += f"{E.warn} Make the bot an <b>admin</b> or membership checks will fail."
        except (TelegramError, ValueError) as exc:
            report = f"{E.no} <b>Test failed</b>\n<code>{ui.esc(exc)}</code>"
        await self.api.send_message(ctx[0], report, ui.kb(self._back("adm:fj")))
        return "Test complete"

    async def in_fj_channel(self, msg: dict, state: dict) -> None:
        chat_id = msg["chat"]["id"]
        parts = (msg.get("text") or "").split()
        if len(parts) != 2 or not parts[0].lstrip("-").isdigit() or not parts[1].startswith("http"):
            await self.api.send_message(chat_id, f"{E.no} Format: <code>channel_id https://t.me/+link</code>")
            return
        self.app.pending.pop(msg["from"]["id"], None)
        await self.db.set_setting("channel_id", parts[0])
        await self.db.set_setting("channel_link", parts[1])
        self.app.join_cache.clear()
        await self.api.send_message(chat_id, f"{E.ok} Channel updated. Use <b>Test</b> to verify.",
                                    ui.kb([ui.btn("Open Force Join", cb="adm:fj", icon="shield", style="primary")]))

    # ── maintenance ───────────────────────────────────────────────────
    async def cb_mt(self, cb, ctx, args):
        if args and args[0] == "t":
            await self.db.set_setting("maintenance", "0" if self.db.get_bool("maintenance") else "1")
        elif args and args[0] == "msg":
            await self._prompt(ctx, cb["from"]["id"], "mt_msg",
                               f"{E.wrench} <b>Maintenance message</b>\nSend the text users will see (or <code>-</code> to reset).")
            return None
        custom = self.db.get_setting("maintenance_msg")
        text = (f"{E.wrench} <b>Maintenance Mode</b>\n{ui.DIV}\n<b>Status:</b> {_onoff(self.db.get_bool('maintenance'))}\n"
                f"<b>Message:</b> {custom or '<i>default</i>'}\n<i>Admins can always use the bot.</i>")
        await self._show(ctx, text, ui.kb(
            [ui.btn("Toggle", cb="adm:mt:t", icon="refresh", style="primary"),
             ui.btn("Set Message", cb="adm:mt:msg", icon="pin")], self._back()))
        return None

    async def in_mt_msg(self, msg: dict, state: dict) -> None:
        self.app.pending.pop(msg["from"]["id"], None)
        raw = msg.get("text") or ""
        value = "" if raw.strip() == "-" else ui.entities_to_html(raw, msg.get("entities"))
        await self.db.set_setting("maintenance_msg", value)
        await self.api.send_message(msg["chat"]["id"], f"{E.ok} Maintenance message saved.",
                                    ui.kb([ui.btn("Open Maintenance", cb="adm:mt", icon="wrench", style="primary")]))

    # ── limits ────────────────────────────────────────────────────────
    async def cb_lim(self, cb, ctx, args):
        uid = cb["from"]["id"]
        if args and args[0] == "daily":
            await self._prompt(ctx, uid, "lim_daily",
                               f"{E.alarm} <b>Daily limit</b>\nSend downloads per user per day (<code>0</code> = unlimited).")
            return None
        if args and args[0] == "cd":
            await self._prompt(ctx, uid, "lim_cd",
                               f"{E.alarm} <b>Cooldown</b>\nSend seconds between requests (<code>0</code> = none).")
            return None
        daily = self.db.get_int("daily_limit")
        text = (f"{E.alarm} <b>Limits</b>\n{ui.DIV}\n"
                f"<b>Daily limit:</b> {daily if daily else 'Unlimited'}\n"
                f"<b>Cooldown:</b> {self.db.get_int('cooldown')}s\n"
                f"<b>Parallel jobs:</b> {self.app.cfg.max_concurrent_jobs}\n<i>Admins are exempt.</i>")
        await self._show(ctx, text, ui.kb(
            [ui.btn("Daily Limit", cb="adm:lim:daily", icon="calendar", style="primary"),
             ui.btn("Cooldown", cb="adm:lim:cd", icon="clock", style="primary")], self._back()))
        return None

    async def _set_number(self, msg: dict, key: str, label: str) -> None:
        text = (msg.get("text") or "").strip()
        if not text.isdigit():
            await self.api.send_message(msg["chat"]["id"], f"{E.no} Send a whole number.")
            return
        self.app.pending.pop(msg["from"]["id"], None)
        await self.db.set_setting(key, text)
        await self.api.send_message(msg["chat"]["id"], f"{E.ok} {label} set to <b>{text}</b>.",
                                    ui.kb([ui.btn("Open Limits", cb="adm:lim", icon="alarm", style="primary")]))

    async def in_lim_daily(self, msg, state):
        await self._set_number(msg, "daily_limit", "Daily limit")

    async def in_lim_cd(self, msg, state):
        await self._set_number(msg, "cooldown", "Cooldown")

    # ── platforms ─────────────────────────────────────────────────────
    async def cb_pf(self, cb, ctx, args):
        if args and args[0] == "t" and len(args) > 1 and args[1] in PLATFORMS:
            key = f"platform_{args[1]}"
            await self.db.set_setting(key, "0" if self.db.get_bool(key) else "1")
        rows = []
        for name in PLATFORMS:
            on = self.db.get_bool(f"platform_{name}")
            rows.append([ui.btn(f"{Platform(name).label}: {'ON' if on else 'OFF'}", cb=f"adm:pf:t:{name}",
                                icon=name if name != "other" else "globe", style="success" if on else "danger")])
        await self._show(ctx, f"{E.globe} <b>Platforms</b>\n{ui.DIV}\nTap to enable / disable a platform for users.",
                         ui.kb(*rows, self._back()))

    # ── welcome text ──────────────────────────────────────────────────
    async def cb_wl(self, cb, ctx, args):
        uid = cb["from"]["id"]
        if args and args[0] == "set":
            await self._prompt(ctx, uid, "wl_set",
                               f"{E.pin} <b>Welcome text</b>\nSend the new message. Formatting and premium emoji are kept.\n"
                               f"Placeholders: <code>{{name}}</code> <code>{{brand}}</code>")
            return None
        if args and args[0] == "reset":
            await self.db.set_setting("welcome", "")
            return "Reset to default"
        if args and args[0] == "prev":
            user = cb["from"]
            await self.api.send_message(
                ctx[0], ui.welcome_text(user.get("first_name") or "friend", self.app.cfg.brand,
                                        self.db.get_setting("welcome")),
                ui.main_menu(True, self.app.channel_link, self.app.cfg.owner_id))
            return None
        state = "custom" if self.db.get_setting("welcome") else "default"
        await self._show(ctx, f"{E.pin} <b>Welcome Text</b>\n{ui.DIV}\nCurrently using the <b>{state}</b> message.",
                         ui.kb([ui.btn("Set New", cb="adm:wl:set", icon="pin", style="success"),
                                ui.btn("Preview", cb="adm:wl:prev", icon="eyes", style="primary")],
                               [ui.btn("Reset", cb="adm:wl:reset", icon="trash", style="danger")], self._back()))
        return None

    async def in_wl_set(self, msg: dict, state: dict) -> None:
        chat_id = msg["chat"]["id"]
        value = ui.entities_to_html(msg.get("text") or "", msg.get("entities"))
        try:
            preview = await self.api.send_message(chat_id, ui.welcome_text(
                msg["from"].get("first_name") or "friend", self.app.cfg.brand, value))
        except TelegramError as exc:
            await self.api.send_message(chat_id, f"{E.no} Invalid formatting: <code>{ui.esc(exc.description)}</code>")
            return
        self.app.pending.pop(msg["from"]["id"], None)
        await self.db.set_setting("welcome", value)
        await self.api.send_message(chat_id, f"{E.ok} Welcome text saved (preview above).",
                                    ui.kb([ui.btn("Open Welcome", cb="adm:wl", icon="pin", style="primary")]),
                                    reply_parameters={"message_id": preview["message_id"]})

    # ── logs ──────────────────────────────────────────────────────────
    async def cb_lg(self, cb, ctx, args):
        if args and args[0] == "clear":
            if len(args) > 1 and args[1] == "yes":
                n = await self.db.clear_logs()
                return f"Cleared {n} log rows"
            await self._show(ctx, f"{E.warn} <b>Clear all download logs?</b>\nUser download counters are kept.",
                             ui.kb([ui.btn("Yes, clear", cb="adm:lg:clear:yes", icon="trash", style="danger"),
                                    ui.btn("Cancel", cb="adm:lg", icon="back", style="primary")]))
            return None
        if args and args[0] in ("recent", "fail"):
            rows = await self.db.recent_downloads(15, failed_only=args[0] == "fail")
            lines = []
            for r in rows:
                icon = E.ok if r["status"] == "ok" else E.no
                when = datetime.fromtimestamp(r["created_at"], self.app.tz).strftime("%d/%m %H:%M")
                extra = f"{r['duration_ms'] / 1000:.1f}s" if r["status"] == "ok" else ui.esc((r["error"] or "")[:60])
                lines.append(f"{icon} <code>{when}</code> {r['platform']} · <code>{r['user_id']}</code> · {extra}")
            title = "Recent failures" if args[0] == "fail" else "Recent downloads"
            await self._show(ctx, f"{E.info} <b>{title}</b>\n{ui.DIV}\n" + ("\n".join(lines) or "Nothing logged yet."),
                             ui.kb(self._back("adm:lg")))
            return None
        await self._show(ctx, f"{E.info} <b>Logs</b>\n{ui.DIV}\nBrowse the latest download activity.",
                         ui.kb([ui.btn("Recent", cb="adm:lg:recent", icon="download", style="primary"),
                                ui.btn("Failures", cb="adm:lg:fail", icon="warn", style="danger")],
                               [ui.btn("Clear Logs", cb="adm:lg:clear", icon="trash")], self._back()))
        return None

    # ── backup / restore ──────────────────────────────────────────────
    async def send_backup(self, chat_id: int, auto: bool = False) -> None:
        stamp = datetime.now(self.app.tz).strftime("%Y%m%d_%H%M")
        path = self.app.tmp_root / f"gx_backup_{stamp}.db"
        await self.db.backup_to(path)
        try:
            label = "Automatic backup" if auto else "Manual backup"
            await self.api.upload("sendDocument", {"document": path}, chat_id=chat_id,
                                  caption=f"{E.backup} <b>{label}</b> • {stamp}", parse_mode="HTML")
            await self.db.set_setting("last_backup", int(time.time()))
        finally:
            path.unlink(missing_ok=True)

    async def cb_bk(self, cb, ctx, args):
        await self.send_backup(ctx[0])
        return "Backup sent"

    async def cb_rs(self, cb, ctx, args):
        uid = cb["from"]["id"]
        if args and args[0] == "go":
            state = self.app.pending.pop(uid, None)
            if not state or state["action"] != "restore_confirm":
                return "Nothing to restore."
            path = Path(state["path"])
            await self.db.restore_from(path)
            path.unlink(missing_ok=True)
            await self.app.reload_admins()
            self.app.apply_settings()
            self.app.join_cache.clear()
            await self._show(ctx, f"{E.ok} <b>Database restored.</b>", ui.kb(self._back()))
            return "Restored"
        await self._prompt(ctx, uid, "restore_wait",
                           f"{E.upload} <b>Restore Database</b>\n{ui.DIV}\nSend a backup <code>.db</code> file.\n"
                           f"{E.warn} This replaces <b>all</b> current data.")
        return None

    async def in_restore_wait(self, msg: dict, state: dict) -> None:
        chat_id, uid = msg["chat"]["id"], msg["from"]["id"]
        doc = msg.get("document")
        if not doc:
            await self.api.send_message(chat_id, f"{E.no} Please send the backup file as a document.")
            return
        dest = self.app.tmp_root / f"restore_{uid}.db"
        try:
            await self.api.download_file(doc["file_id"], dest)
            info = Database.validate_file(dest)
        except (TelegramError, ValueError) as exc:
            dest.unlink(missing_ok=True)
            self.app.pending.pop(uid, None)
            await self.api.send_message(chat_id, f"{E.no} {ui.esc(getattr(exc, 'description', exc))}",
                                        ui.kb(self._back()))
            return
        self.app.pending[uid] = {"action": "restore_confirm", "path": str(dest)}
        await self.api.send_message(
            chat_id, f"{E.warn} <b>Replace current data?</b>\nBackup contains <b>{info['users']}</b> users and "
                     f"<b>{info['downloads']}</b> log rows.",
            ui.kb([ui.btn("Restore", cb="adm:rs:go", icon="ok", style="danger"),
                   ui.btn("Cancel", cb="adm:cancel", icon="no", style="primary")]))

    async def in_restore_confirm(self, msg: dict, state: dict) -> None:
        await self.api.send_message(msg["chat"]["id"], "Use the buttons above, or /cancel.")

    # ── health ────────────────────────────────────────────────────────
    async def cb_hl(self, cb, ctx, args):
        t0 = time.monotonic()
        with contextlib.suppress(TelegramError):
            await self.api.call("getMe", _timeout=10)
        tg_ms = int((time.monotonic() - t0) * 1000)
        t0 = time.monotonic()
        online = await self.app.svc.online()
        be_ms = int((time.monotonic() - t0) * 1000)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        poll_age = int(time.time() - self.app.last_poll_ok)
        text = (
            f"{E.green} <b>System Health</b>\n{ui.DIV}\n"
            f"{'🟢' if poll_age < 60 else '🔴'} <b>Polling:</b> last update {poll_age}s ago\n"
            f"{'🟢' if online else '🔴'} <b>Backend:</b> {'online' if online else 'offline'} ({be_ms} ms)\n"
            f"{E.bolt} <b>Telegram API:</b> {tg_ms} ms\n"
            f"{E.backup} <b>Database:</b> {ui.fmt_size(await self.db.size_bytes())}\n"
            f"{ui.DIV}\n"
            f"{E.clock} <b>Uptime:</b> {ui.fmt_duration(time.time() - self.app.started)}\n"
            f"{E.download} <b>Active jobs:</b> {len(self.app.active)}\n"
            f"{E.gear} <b>Memory:</b> {rss:.0f} MB · Python {sys.version.split()[0]}\n"
            f"{E.sparkles} <b>Premium emoji:</b> {_onoff(emoji.premium_enabled())}\n"
            f"{E.shield} <b>Force join:</b> {_onoff(self.db.get_bool('force_join'))}"
        )
        await self._show(ctx, text, ui.kb([ui.btn("Refresh", cb="adm:hl", icon="refresh", style="success")], self._back()))

    # ── emoji test ────────────────────────────────────────────────────
    async def cb_em(self, cb, ctx, args):
        names = emoji.all_names()
        chunk = []
        for i in range(0, len(names), 3):
            line = "   ".join(f"{getattr(E, n)} <code>{n}</code>" for n in names[i:i + 3])
            chunk.append(line)
        header = (f"{E.sparkles} <b>Emoji Test</b>\n{ui.DIV}\n"
                  f"If you only see plain emoji, the bot owner needs Telegram Premium.\n\n")
        pages, current = [], header
        for line in chunk:
            if len(current) + len(line) > 3500:
                pages.append(current)
                current = ""
            current += line + "\n"
        pages.append(current)
        for page in pages:
            await self.api.send_message(ctx[0], page)
        await self.api.send_message(
            ctx[0], f"{E.eyes} <b>Colored buttons test</b>",
            ui.kb([ui.btn("Success", cb="noop", icon="ok", style="success"),
                   ui.btn("Danger", cb="noop", icon="no", style="danger"),
                   ui.btn("Primary", cb="noop", icon="star", style="primary")],
                  [ui.btn("YouTube", cb="noop", icon="youtube"), ui.btn("TikTok", cb="noop", icon="tiktok")],
                  [ui.btn("Instagram", cb="noop", icon="instagram"), ui.btn("Facebook", cb="noop", icon="facebook")],
                  self._back()))
        return "Sent"

    # ── admins ────────────────────────────────────────────────────────
    async def cb_ad(self, cb, ctx, args):
        uid = cb["from"]["id"]
        if args and args[0] in ("add", "rm"):
            verb = "add" if args[0] == "add" else "remove"
            await self._prompt(ctx, uid, f"ad_{args[0]}",
                               f"{E.crown} <b>{verb.title()} admin</b>\nSend the user's numeric <b>ID</b>.")
            return None
        lines = [f"{E.crown} <a href=\"tg://user?id={self.app.cfg.owner_id}\">Owner</a> "
                 f"<code>{self.app.cfg.owner_id}</code>"]
        for admin_id in sorted(self.app.admin_ids):
            lines.append(f"{E.user} <a href=\"tg://user?id={admin_id}\">Admin</a> <code>{admin_id}</code>")
        await self._show(ctx, f"{E.crown} <b>Admins</b>\n{ui.DIV}\n" + "\n".join(lines), ui.kb(
            [ui.btn("Add Admin", cb="adm:ad:add", icon="plus", style="success"),
             ui.btn("Remove", cb="adm:ad:rm", icon="trash", style="danger")], self._back()))
        return None

    async def in_ad_add(self, msg: dict, state: dict) -> None:
        chat_id, text = msg["chat"]["id"], (msg.get("text") or "").strip()
        self.app.pending.pop(msg["from"]["id"], None)
        if not text.isdigit():
            await self.api.send_message(chat_id, f"{E.no} Send a numeric user ID.", ui.kb(self._back("adm:ad")))
            return
        await self.db.add_admin(int(text), msg["from"]["id"])
        await self.app.reload_admins()
        await self.api.send_message(chat_id, f"{E.ok} <code>{text}</code> is now an admin.", ui.kb(self._back("adm:ad")))

    async def in_ad_rm(self, msg: dict, state: dict) -> None:
        chat_id, text = msg["chat"]["id"], (msg.get("text") or "").strip()
        self.app.pending.pop(msg["from"]["id"], None)
        ok = text.isdigit() and await self.db.remove_admin(int(text))
        await self.app.reload_admins()
        await self.api.send_message(chat_id, f"{E.ok} Removed." if ok else f"{E.no} Not an admin.",
                                    ui.kb(self._back("adm:ad")))

    # ── settings ──────────────────────────────────────────────────────
    async def cb_st(self, cb, ctx, args):
        if args and args[0] == "t" and len(args) > 1 and args[1] in TOGGLES:
            key = args[1]
            await self.db.set_setting(key, "0" if self.db.get_bool(key) else "1")
            if key == "premium_emoji":
                self.app.apply_settings()
        rows = []
        for key, label in TOGGLES.items():
            on = self.db.get_bool(key)
            rows.append([ui.btn(f"{label}: {'ON' if on else 'OFF'}", cb=f"adm:st:t:{key}",
                                style="success" if on else "danger")])
        await self._show(ctx, f"{E.gear} <b>Settings</b>\n{ui.DIV}\nTap a switch to toggle it.",
                         ui.kb(*rows, self._back()))
