"""Texts, keyboards and formatting helpers (everything the user sees)."""
from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import emoji
from download import Platform
from emoji import E

DIV = "━━━━━━━━━━━━━━━━━━━━"
SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


# ── helpers ───────────────────────────────────────────────────────────────
def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=False)


def bar(pct: int, width: int = 10) -> str:
    filled = max(0, min(width, round(width * pct / 100)))
    return "▰" * filled + "▱" * (width - filled)


def fmt_ts(ts: int | float | None, tz: str) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%d %b %Y, %H:%M")


def fmt_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return f"{num:.1f} GB"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    return " ".join(p for p in (f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m" if m else "", f"{s}s") if p)


def platform_emoji(platform: Platform) -> str:
    return {Platform.YOUTUBE: E.youtube, Platform.TIKTOK: E.tiktok,
            Platform.INSTAGRAM: E.instagram, Platform.FACEBOOK: E.facebook}.get(platform, E.globe)


def btn(label: str, *, cb: str | None = None, url: str | None = None,
        icon: str | None = None, style: str | None = None) -> dict:
    """Inline button. ``style`` is one of primary / success / danger (Bot API 9.4)."""
    button: dict = {"text": label}
    if icon:
        custom = emoji.icon_id(icon)
        if custom:
            button["icon_custom_emoji_id"] = custom
        else:
            button["text"] = f"{emoji.fallback(icon)} {label}"
    if style:
        button["style"] = style
    if url:
        button["url"] = url
    else:
        button["callback_data"] = cb or "noop"
    return button


def kb(*rows: list[dict]) -> dict:
    return {"inline_keyboard": [list(r) for r in rows]}


def entities_to_html(text: str, entities: list[dict] | None) -> str:
    """Convert a Telegram message (text + entities) to HTML, keeping custom emoji."""
    if not entities:
        return html.escape(text, quote=False)
    simple = {
        "bold": ("<b>", "</b>"), "italic": ("<i>", "</i>"), "underline": ("<u>", "</u>"),
        "strikethrough": ("<s>", "</s>"), "spoiler": ("<tg-spoiler>", "</tg-spoiler>"),
        "code": ("<code>", "</code>"), "pre": ("<pre>", "</pre>"),
        "blockquote": ("<blockquote>", "</blockquote>"),
        "expandable_blockquote": ("<blockquote expandable>", "</blockquote>"),
    }

    def tags(ent: dict) -> tuple[str, str] | None:
        kind = ent.get("type")
        if kind in simple:
            return simple[kind]
        if kind == "text_link" and ent.get("url"):
            return f'<a href="{html.escape(ent["url"])}">', "</a>"
        if kind == "custom_emoji" and ent.get("custom_emoji_id"):
            return f'<tg-emoji emoji-id="{ent["custom_emoji_id"]}">', "</tg-emoji>"
        return None

    starts: dict[int, list[tuple[str, str]]] = defaultdict(list)
    ends: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for ent in sorted(entities, key=lambda e: (e["offset"], -e["length"])):
        pair = tags(ent)
        if pair:
            starts[ent["offset"]].append(pair)
            ends[ent["offset"] + ent["length"]].append(pair)

    out: list[str] = []
    pos = 0                                     # UTF-16 code-unit position
    for ch in text:
        for _, close in reversed(ends.get(pos, [])):
            out.append(close)
        for opening, _ in starts.get(pos, []):
            out.append(opening)
        out.append(html.escape(ch, quote=False))
        pos += 2 if ord(ch) > 0xFFFF else 1
    for _, close in reversed(ends.get(pos, [])):
        out.append(close)
    return "".join(out)


# ── user-facing texts ─────────────────────────────────────────────────────
def welcome_text(name: str, brand: str, custom: str = "") -> str:
    if custom.strip():
        return custom.replace("{name}", esc(name)).replace("{brand}", esc(brand))
    return (
        f"{E.rocket} <b>Welcome to {esc(brand)}, {esc(name)}!</b>\n{DIV}\n"
        f"<blockquote>{E.bolt} Universal media downloader — HD video, MP3 audio and photo "
        f"slideshows with <b>no watermark</b>.</blockquote>\n\n"
        f"{E.youtube} YouTube   {E.tiktok} TikTok\n"
        f"{E.instagram} Instagram   {E.facebook} Facebook\n\n"
        f"{E.link} <b>Send me a link</b> and I'll fetch it instantly."
    )


def boot_frame(pct: int, label: str) -> str:
    return f"{E.bolt} <b>{label}</b>\n<code>{bar(pct)} {pct}%</code>"


def join_text() -> str:
    return (
        f"{E.lock} <b>Channel Membership Required</b>\n{DIV}\n"
        f"<blockquote>To keep using this bot, please join our official channel.</blockquote>\n\n"
        f"{E.megaphone} Tap <b>Join Channel</b>, then press <b>I've Joined</b> to verify."
    )


def guide_text() -> str:
    return (
        f"{E.bulb} <b>How to Download</b>\n{DIV}\n"
        f"<b>1.</b> Copy the link of a video or post\n"
        f"<b>2.</b> Paste it here in the chat\n"
        f"<b>3.</b> Wait a few seconds — the file arrives automatically\n\n"
        f"<blockquote>{E.info} Only <b>public</b> content can be downloaded. "
        f"Files up to <b>50 MB</b> are sent directly; larger ones come as a download link.</blockquote>"
    )


def platforms_text() -> str:
    return (
        f"{E.globe} <b>Supported Platforms</b>\n{DIV}\n"
        f"{E.youtube} <b>YouTube</b> — videos, shorts, audio\n"
        f"{E.tiktok} <b>TikTok</b> — no watermark, slideshows\n"
        f"{E.instagram} <b>Instagram</b> — reels, posts, carousels\n"
        f"{E.facebook} <b>Facebook</b> — videos &amp; reels\n\n"
        f"<blockquote>{E.sparkles} Other sites may work too — just send the link.</blockquote>"
    )


def faq_text() -> str:
    return (
        f"{E.question} <b>FAQ</b>\n{DIV}\n"
        f"<b>Is it free?</b>\nYes — 100% free, no registration.\n\n"
        f"<b>Is there a watermark?</b>\nNo. You get the original clean file.\n\n"
        f"<b>Why did my download fail?</b>\nMake sure the account is public and the link is correct.\n\n"
        f"<b>Can I download private videos?</b>\nNo, only public content.\n\n"
        f"<b>Which formats?</b>\nMP4 video, MP3 audio and JPG photo slideshows."
    )


def profile_text(user: dict, tz: str, used_today: int, limit: int, is_admin: bool) -> str:
    uname = f"@{esc(user['username'])}" if user.get("username") else "—"
    quota = "Unlimited" if (limit <= 0 or is_admin) else f"{used_today}/{limit} today"
    role = f"{E.crown} Admin" if is_admin else f"{E.user} Member"
    return (
        f"{E.user} <b>My Profile</b>\n{DIV}\n"
        f"<b>Name:</b> {esc(user.get('first_name') or '')}\n"
        f"<b>Username:</b> {uname}\n"
        f"<b>ID:</b> <code>{user['user_id']}</code>\n"
        f"<b>Role:</b> {role}\n"
        f"<b>Joined:</b> {fmt_ts(user['joined_at'], tz)}\n"
        f"<b>Downloads:</b> {user['downloads']}\n"
        f"<b>Quota:</b> {quota}"
    )


def banned_text(reason: str = "") -> str:
    extra = f"\n<b>Reason:</b> {esc(reason)}" if reason else ""
    return f"{E.stop} <b>Access Restricted</b>\n{DIV}\nYou have been banned from using this bot.{extra}"


def maintenance_text(custom: str = "") -> str:
    body = custom.strip() or "We're improving the service. Please check back soon."
    return f"{E.wrench} <b>Under Maintenance</b>\n{DIV}\n<blockquote>{body}</blockquote>"


def limit_text(limit: int) -> str:
    return (f"{E.clock} <b>Daily Limit Reached</b>\n{DIV}\n"
            f"You've used all <b>{limit}</b> downloads for today. Come back tomorrow!")


def cooldown_text(seconds: int) -> str:
    return f"{E.alarm} <b>Slow down!</b> Please wait <b>{seconds}s</b> before the next request."


def busy_text() -> str:
    return f"{E.loading} <b>One at a time!</b> Your previous download is still in progress."


def invalid_link_text() -> str:
    return (f"{E.link} <b>Send a valid link</b>\n{DIV}\n"
            f"Paste a video or post URL (YouTube, TikTok, Instagram, Facebook…).")


def platform_disabled_text(platform: Platform) -> str:
    return (f"{E.pause} <b>{platform.label} is temporarily disabled.</b>\n"
            f"Please try again later.")


def error_text(message: str) -> str:
    return (f"{E.no} <b>Download failed</b>\n{DIV}\n<blockquote>{esc(message)}</blockquote>\n"
            f"{E.bulb} Check that the link is public and try again.")


def progress_text(platform: Platform, label: str, pct: int, tick: int) -> str:
    return (
        f"{platform_emoji(platform)} <b>{platform.label} Download</b>\n{DIV}\n"
        f"<code>{SPINNER[tick % len(SPINNER)]}</code> {esc(label)}\n"
        f"<code>{bar(pct)} {pct}%</code>"
    )


def caption(platform: Platform, bot_username: str) -> str:
    return f"{platform_emoji(platform)} <b>{platform.label}</b> • via @{esc(bot_username)}"


def too_large_text(size: int, link: str) -> str:
    return (
        f"{E.warn} <b>File too large for Telegram</b>\n{DIV}\n"
        f"This file is <b>{fmt_size(size)}</b> — above the 50 MB bot upload limit.\n"
        f"{E.download} <a href=\"{html.escape(link)}\">Download directly</a>"
    )


# ── keyboards ─────────────────────────────────────────────────────────────
def main_menu(is_admin: bool, channel_link: str, owner_id: int) -> dict:
    rows = [
        [btn("How to Download", cb="menu:guide", icon="bulb", style="primary"),
         btn("Platforms", cb="menu:platforms", icon="globe", style="primary")],
        [btn("My Profile", cb="menu:profile", icon="user", style="success"),
         btn("FAQ", cb="menu:faq", icon="question", style="primary")],
        [btn("Channel", url=channel_link, icon="megaphone"),
         btn("Support", url=f"tg://user?id={owner_id}", icon="mail")],
    ]
    if is_admin:
        rows.append([btn("Admin Panel", cb="adm:home", icon="crown", style="primary")])
    return kb(*rows)


def back_menu() -> dict:
    return kb([btn("Back to Menu", cb="menu:home", icon="back", style="primary")])


def join_keyboard(channel_link: str) -> dict:
    return kb(
        [btn("Join Channel", url=channel_link, icon="megaphone", style="primary")],
        [btn("I've Joined", cb="check_join", icon="ok", style="success")],
    )


def result_keyboard(audio_key: str | None, source_url: str | None) -> dict | None:
    row: list[dict] = []
    if audio_key:
        row.append(btn("Get Audio", cb=f"aud:{audio_key}", icon="audio", style="success"))
    if source_url and len(source_url) <= 2000:
        row.append(btn("Source", url=source_url, icon="link"))
    return kb(row) if row else None
