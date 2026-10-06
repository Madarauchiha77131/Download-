"""Premium (custom) emoji registry.

Every emoji has a plain-Unicode fallback, so the bot keeps working when the
owner has no Telegram Premium, or when premium emoji are switched off in the
admin panel.

Usage::

    from emoji import E
    text = f"{E.youtube} <b>YouTube</b>"      # -> <tg-emoji ...>▶️</tg-emoji>
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Emoji:
    fallback: str
    custom_id: str | None = None


_enabled = True


def set_premium_enabled(value: bool) -> None:
    global _enabled
    _enabled = bool(value)


def premium_enabled() -> bool:
    return _enabled


REGISTRY: dict[str, Emoji] = {
    # ── Platforms (supplied by the owner) ─────────────────────────────
    "youtube": Emoji("▶️", "5069108689810490417"),
    "facebook": Emoji("📘", "5933728045067145810"),
    "instagram": Emoji("📸", "6039590179935620964"),
    "tiktok": Emoji("🎵", "6021412619913532794"),
    # ── ProtectStatus pack ────────────────────────────────────────────
    "shield": Emoji("🛡", "5902016123972358349"),
    "phone": Emoji("📱", "5895652322469482989"),
    "ok": Emoji("✅", "5895514131896733546"),
    "no": Emoji("❌", "5893163582194978381"),
    "clock": Emoji("🕒", "5893102202817352158"),
    "alarm": Emoji("⏰", "5902050947567194830"),
    "plane": Emoji("✈️", "5893333516871012690"),
    "call": Emoji("📞", "5893297890117292323"),
    "briefcase": Emoji("💼", "5893255507380014983"),
    "sleep": Emoji("💤", "5893281616486209299"),
    "money": Emoji("💰", "5893473283696759404"),
    "search": Emoji("🔎", "5893382531037794941"),
    "pin": Emoji("📌", "5895440460322706085"),
    "bulb": Emoji("💡", "5893290369629556374"),
    "music": Emoji("🎶", "5892966078123872045"),
    "chart": Emoji("📊", "5895444149699612825"),
    "excl": Emoji("❗", "5893072412924187198"),
    "victory": Emoji("✌️", "5895338626648117927"),
    "trophy": Emoji("🏆", "5893376775781617954"),
    "sparkle": Emoji("💫", "5895770017458294953"),
    "star": Emoji("⭐", "5893494861612455015"),
    "bolt": Emoji("⚡", "5893450623449305489"),
    "sparkles": Emoji("✨", "5893321843149902412"),
    "fire": Emoji("🔥", "5893185207355315979"),
    "globe": Emoji("🌐", "6039450035152753195"),
    "warn": Emoji("⚠️", "5904692292324692386"),
    "card": Emoji("💳", "5902056028513505203"),
    "plus": Emoji("➕", "5902453596456227896"),
    "stop": Emoji("⛔", "5904238507555033712"),
    "trash": Emoji("🗑", "5904542823167824187"),
    "rocket": Emoji("🚀", "6041705726206808304"),
    "gear": Emoji("⚙️", "5893161718179173515"),
    "user": Emoji("👤", "5902335789798265487"),
    "link": Emoji("🔗", "5902449142575141204"),
    "soon": Emoji("🔜", "5893368370530621889"),
    # ── NewsEmoji pack ────────────────────────────────────────────────
    "download": Emoji("⬇️", "5406745015365943482"),
    "upload": Emoji("⬆️", "5415655814079723871"),
    "loading": Emoji("⏳", "5386367538735104399"),
    "refresh": Emoji("🔄", "5375338737028841420"),
    "megaphone": Emoji("📣", "5424818078833715060"),
    "lock": Emoji("🔐", "5296369303661067030"),
    "info": Emoji("ℹ️", "5334544901428229844"),
    "question": Emoji("❓", "5452069934089641166"),
    "heart": Emoji("❤️", "5337080053119336309"),
    "diamond": Emoji("💎", "5427168083074628963"),
    "mail": Emoji("📬", "5253742260054409879"),
    "wrench": Emoji("🔧", "5341715473882955310"),
    "laptop": Emoji("💻", "5282843764451195532"),
    "calendar": Emoji("📅", "5413879192267805083"),
    "green": Emoji("🟢", "5416081784641168838"),
    "red": Emoji("🔴", "5411225014148014586"),
    "speaker": Emoji("🔊", "5388632425314140043"),
    "free": Emoji("🆓", "5406756500108501710"),
    "siren": Emoji("🚨", "5395695537687123235"),
    "play": Emoji("▶️", "5264919878082509254"),
    "pause": Emoji("⏸", "5359543311897998264"),
    "new": Emoji("🆕", "5382357040008021292"),
    "trend": Emoji("📈", "5244837092042750681"),
    "eyes": Emoji("👀", "5210956306952758910"),
    "party": Emoji("🥳", "5461151367559141950"),
    "bell": Emoji("🔔", "5458603043203327669"),
    # ── Mysteriousashb pack ───────────────────────────────────────────
    "crown": Emoji("👑", "6310083389126876253"),
    # ── Semantic aliases (re-use ids above) ───────────────────────────
    "video": Emoji("🎬", "5264919878082509254"),
    "audio": Emoji("🎧", "5892966078123872045"),
    "users": Emoji("👥", "5902335789798265487"),
    # ── No custom id available: plain Unicode everywhere ──────────────
    "photo": Emoji("🖼"),
    "back": Emoji("◀️"),
    "file": Emoji("📄"),
    "backup": Emoji("💾"),
}


def tg(name: str) -> str:
    """HTML snippet for message text / captions."""
    em = REGISTRY[name]
    if _enabled and em.custom_id:
        return f'<tg-emoji emoji-id="{em.custom_id}">{em.fallback}</tg-emoji>'
    return em.fallback


def icon_id(name: str) -> str | None:
    """Custom emoji id for ``icon_custom_emoji_id`` on buttons (or None)."""
    em = REGISTRY[name]
    return em.custom_id if (_enabled and em.custom_id) else None


def fallback(name: str) -> str:
    return REGISTRY[name].fallback


def all_names() -> list[str]:
    return list(REGISTRY)


class _Emojis:
    """Attribute access: ``E.fire`` -> HTML for the fire emoji."""

    def __getattr__(self, name: str) -> str:
        try:
            return tg(name)
        except KeyError as exc:
            raise AttributeError(name) from exc


E = _Emojis()
