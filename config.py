"""Central configuration.

Secrets (the bot token) come from environment variables. Everything else has a
sensible default baked in, so on Render you only have to set ``BOT_TOKEN``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OWNER_ID = 8200980090
DEFAULT_CHANNEL_ID = -1002740009398
DEFAULT_CHANNEL_LINK = "https://t.me/+2Fxg6o4jEKAxOGQ1"
DEFAULT_BACKEND_URL = "https://fvz.onrender.com"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise SystemExit(f"Environment variable {name} must be an integer, got {raw!r}")


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    channel_id: int
    channel_link: str
    backend_url: str
    brand: str
    port: int
    db_path: Path
    tmp_dir: Path
    timezone: str
    max_concurrent_jobs: int
    log_level: str


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN environment variable is required.")
    return Config(
        bot_token=token,
        owner_id=_env_int("OWNER_ID", DEFAULT_OWNER_ID),
        channel_id=_env_int("CHANNEL_ID", DEFAULT_CHANNEL_ID),
        channel_link=os.getenv("CHANNEL_LINK", DEFAULT_CHANNEL_LINK).strip(),
        backend_url=os.getenv("BACKEND_URL", DEFAULT_BACKEND_URL).strip().rstrip("/"),
        brand=os.getenv("BRAND", "GX").strip() or "GX",
        port=_env_int("PORT", 10000),
        db_path=Path(os.getenv("DATABASE_PATH", "data/bot.db")),
        tmp_dir=Path(os.getenv("TMP_DIR", "/tmp/gx_bot")),
        timezone=os.getenv("TIMEZONE", "Asia/Dhaka").strip(),
        max_concurrent_jobs=max(1, _env_int("MAX_CONCURRENT_JOBS", 4)),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )
