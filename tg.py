"""Thin, dependency-free Telegram Bot API client built on aiohttp.

A raw client (instead of a framework) guarantees that Bot API 9.4 features such
as coloured buttons (``style``) and ``icon_custom_emoji_id`` work regardless of
which library version is installed.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger("tg")

_TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)
_EMOJI_ERR_HINTS = ("emoji", "icon", "entit", "button_")


class TelegramError(Exception):
    def __init__(self, method: str, description: str, code: int = 0, retry_after: int = 0):
        super().__init__(f"{method}: [{code}] {description}")
        self.method = method
        self.description = description
        self.code = code
        self.retry_after = retry_after

    @property
    def blocked(self) -> bool:
        return self.code == 403

    @property
    def not_modified(self) -> bool:
        return "not modified" in self.description.lower()


# ── premium-emoji fallbacks ───────────────────────────────────────────────
def _strip_icons(markup: Any) -> Any:
    if isinstance(markup, str):
        try:
            return json.dumps(_strip_icons(json.loads(markup)))
        except ValueError:
            return markup
    markup = copy.deepcopy(markup)
    for key in ("inline_keyboard", "keyboard"):
        for row in markup.get(key, []) if isinstance(markup, dict) else []:
            for button in row:
                if isinstance(button, dict):
                    button.pop("icon_custom_emoji_id", None)
    return markup


def strip_premium(params: dict[str, Any]) -> dict[str, Any]:
    """Return params with custom emoji replaced by their plain fallbacks."""
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in ("text", "caption") and isinstance(value, str):
            value = _TG_EMOJI_RE.sub(r"\1", value)
        elif key == "reply_markup" and value:
            value = _strip_icons(value)
        out[key] = value
    return out


def _looks_like_emoji_error(err: TelegramError) -> bool:
    if err.code != 400:
        return False
    low = err.description.lower()
    return any(hint in low for hint in _EMOJI_ERR_HINTS)


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


class TelegramAPI:
    def __init__(self, token: str, session: aiohttp.ClientSession):
        self._token = token
        self._session = session
        self._base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"

    # ── core calls ────────────────────────────────────────────────────
    async def call(self, method: str, _timeout: float = 30, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        stripped = False
        for attempt in range(4):
            try:
                async with self._session.post(
                    f"{self._base}/{method}", json=params,
                    timeout=aiohttp.ClientTimeout(total=_timeout),
                ) as resp:
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                raise TelegramError(method, f"network error: {exc!r}") from exc
            if data.get("ok"):
                return data["result"]
            err = TelegramError(
                method, data.get("description", "unknown error"),
                data.get("error_code", 0),
                (data.get("parameters") or {}).get("retry_after", 0),
            )
            if err.code == 429 and attempt < 3:
                await asyncio.sleep(err.retry_after + 0.5)
                continue
            if not stripped and _looks_like_emoji_error(err):
                log.warning("Premium emoji rejected (%s) - retrying with fallbacks", err.description)
                params, stripped = strip_premium(params), True
                continue
            raise err
        raise TelegramError(method, "too many retries")

    async def upload(self, method: str, files: dict[str, Path], _timeout: float = 600, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        stripped = False
        for attempt in range(4):
            handles = []
            try:
                form = aiohttp.FormData()
                for key, value in params.items():
                    form.add_field(key, _form_value(value))
                for field, path in files.items():
                    fh = path.open("rb")
                    handles.append(fh)
                    form.add_field(field, fh, filename=path.name)
                try:
                    async with self._session.post(
                        f"{self._base}/{method}", data=form,
                        timeout=aiohttp.ClientTimeout(total=_timeout),
                    ) as resp:
                        data = await resp.json(content_type=None)
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    raise TelegramError(method, f"network error: {exc!r}") from exc
            finally:
                for fh in handles:
                    fh.close()
            if data.get("ok"):
                return data["result"]
            err = TelegramError(
                method, data.get("description", "unknown error"),
                data.get("error_code", 0),
                (data.get("parameters") or {}).get("retry_after", 0),
            )
            if err.code == 429 and attempt < 3:
                await asyncio.sleep(err.retry_after + 0.5)
                continue
            if not stripped and _looks_like_emoji_error(err):
                params, stripped = strip_premium(params), True
                continue
            raise err
        raise TelegramError(method, "too many retries")

    async def download_file(self, file_id: str, dest: Path, max_bytes: int = 20 * 1024 * 1024) -> Path:
        info = await self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > max_bytes:
            raise TelegramError("getFile", "file too large")
        try:
            async with self._session.get(
                f"{self._file_base}/{info['file_path']}",
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    raise TelegramError("download", f"HTTP {resp.status}", resp.status)
                dest.write_bytes(await resp.read())
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise TelegramError("download", f"network error: {exc!r}") from exc
        return dest

    # ── convenience helpers ───────────────────────────────────────────
    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None, **kw: Any) -> dict:
        return await self.call(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
            link_preview_options={"is_disabled": True}, reply_markup=reply_markup, **kw,
        )

    async def edit_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> dict | None:
        try:
            return await self.call(
                "editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                parse_mode="HTML", link_preview_options={"is_disabled": True},
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            if exc.not_modified:
                return None
            raise

    async def answer(self, callback_id: str, text: str | None = None, alert: bool = False) -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id,
                            text=text, show_alert=alert or None, _timeout=10)
        except TelegramError as exc:
            log.debug("answerCallbackQuery failed: %s", exc)

    async def delete(self, chat_id: int, message_id: int) -> None:
        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=message_id, _timeout=10)
        except TelegramError:
            pass

    async def action(self, chat_id: int, action: str = "typing") -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action=action, _timeout=10)
        except TelegramError:
            pass
