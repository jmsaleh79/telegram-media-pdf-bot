"""Hormuz — multilingual Telegram media bot served through a FastAPI webhook.

Menu map (inline keyboards, one message that is edited in place)::

    🏠 Home
     ├─ 📥 Download from link ........ send a link -> quality picker -> download
     ├─ 🔍 YouTube search ............ type a name -> result buttons -> download
     ├─ 🎵 Find a song ............... type a name -> result buttons -> MP3
     ├─ 🧰 Tools
     │    └─ 📄 Word/Excel -> PDF
     ├─ 🎬 Movies & Series ........... "coming soon" popup
     ├─ ⚙️ Settings
     │    └─ 🌐 Language
     └─ ℹ️ Help
"""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from converter import SUPPORTED_EXTENSIONS, convert_office_to_pdf
from downloader import (
    MAX_MEDIA_BYTES,
    MediaTimeoutError,
    MediaTooLargeError,
    audio_bitrate_ladder,
    classify_media_error,
    download_media,
    fallback_qualities,
    format_duration,
    safe_filename,
    search_songs,
    search_youtube,
    validate_media_url,
)
from locales import COMMAND_DESCRIPTIONS, LANGUAGES, get_text

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("telegram_media_bot")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "/tmp/telegram_bot.sqlite3"))

MAX_UPLOAD_BYTES = MAX_MEDIA_BYTES
MAX_INCOMING_BYTES = 19 * 1024 * 1024
UPLOAD_TIMEOUT = 600          # seconds allowed for pushing a file to Telegram
SEARCH_TIMEOUT = 60           # seconds allowed for a YouTube / SoundCloud search
YOUTUBE_RESULT_LIMIT = 8
SONG_RESULT_LIMIT = 5
DEFAULT_SEARCH_QUALITY = "720p"
VALID_QUALITIES = {"best", "720p", "480p", "360p", "audio"}

_DOWNLOAD_SLOTS = asyncio.Semaphore(2)   # at most 2 downloads at once (free-tier friendly)
_CONVERT_SLOTS = asyncio.Semaphore(1)    # LibreOffice is heavy: one conversion at a time
_ACTIVE_USERS: set[int] = set()          # one running download per user
_BACKGROUND_TASKS: set[asyncio.Task] = set()


# --------------------------------------------------------------------------- #
# Language preferences
# --------------------------------------------------------------------------- #
class UserStore:
    """Small SQLite/PostgreSQL store for language preferences (with an in-memory cache)."""

    def __init__(self, path: Path):
        self.path = path
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        if self.database_url.startswith("postgres://"):
            self.database_url = "postgresql://" + self.database_url[len("postgres://"):]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: dict[int, str] = {}
        self._schema_ready = False
        self._ensure_schema()

    def _connect(self):
        if self.database_url:
            import psycopg

            return psycopg.connect(self.database_url)
        return sqlite3.connect(self.path)

    @property
    def _placeholder(self) -> str:
        return "%s" if self.database_url else "?"

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS user_preferences "
                    "(user_id BIGINT PRIMARY KEY, language TEXT NOT NULL)"
                )
                connection.commit()
            self._schema_ready = True
        except Exception:  # the bot must still start if the database is briefly down
            LOGGER.exception("Could not prepare the preferences table; will retry on next use")

    def cached_language(self, user_id: int) -> str | None:
        return self._cache.get(user_id)

    def get_language(self, user_id: int) -> str | None:
        cached = self._cache.get(user_id)
        if cached:
            return cached
        self._ensure_schema()
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    f"SELECT language FROM user_preferences WHERE user_id = {self._placeholder}",
                    (user_id,),
                ).fetchone()
        except Exception:
            LOGGER.warning("Could not read the language preference", exc_info=True)
            return None
        language = row[0] if row and row[0] in LANGUAGES else None
        if language:
            self._cache[user_id] = language
        return language

    def set_language(self, user_id: int, language: str) -> None:
        if language not in LANGUAGES:
            raise ValueError("Unsupported language")
        self._cache[user_id] = language  # works for this session even if the DB write fails
        self._ensure_schema()
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT INTO user_preferences(user_id, language) VALUES("
                    + self._placeholder + ", " + self._placeholder + ") "
                    "ON CONFLICT(user_id) DO UPDATE SET language = excluded.language",
                    (user_id, language),
                )
                connection.commit()
        except Exception:
            LOGGER.warning("Could not persist the language preference", exc_info=True)


store = UserStore(DATABASE_PATH)


async def stored_language(user_id: int) -> str | None:
    cached = store.cached_language(user_id)
    if cached:
        return cached
    return await asyncio.to_thread(store.get_language, user_id)


def guess_language(user: Any) -> str:
    """Pick a sensible default from Telegram's language_code (ar / ru / else en)."""
    code = (getattr(user, "language_code", "") or "").lower()
    for language in LANGUAGES:
        if code == language or code.startswith(language + "-"):
            return language
    return "en"


async def user_language(user: Any) -> str:
    return await stored_language(user.id) or guess_language(user)


# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #
def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _fit_label(prefix: str, text: str, suffix: str = "", limit: int = 60) -> str:
    """Trim ``text`` so ``prefix + text + suffix`` fits ``limit`` (the suffix always survives)."""
    text = " ".join(str(text).split())
    room = limit - len(prefix) - len(suffix)
    if len(text) > room:
        text = text[: max(1, room - 1)].rstrip() + "…"
    return f"{prefix}{text}{suffix}"


def language_keyboard(
    back: str | None = None, language: str = "en", current: str | None = None
) -> InlineKeyboardMarkup:
    rows = []
    for code, (label, _) in LANGUAGES.items():
        rows.append([btn(f"✅ {label}" if code == current else label, f"lang|{code}")])
    if back:
        rows.append([btn(get_text(language, "menu_back"), f"nav|{back}")])
    return InlineKeyboardMarkup(rows)


def main_menu_keyboard(language: str) -> InlineKeyboardMarkup:
    def t(key: str) -> str:
        return get_text(language, key)

    return InlineKeyboardMarkup([
        [btn(t("menu_download"), "nav|download"), btn(t("menu_youtube_search"), "nav|ytsearch")],
        [btn(t("menu_audio_search"), "nav|song"), btn(t("menu_tools"), "nav|tools")],
        [btn(t("menu_movies"), "nav|movies")],
        [btn(t("menu_settings"), "nav|settings"), btn(t("menu_help"), "nav|help")],
    ])


def back_keyboard(language: str, target: str = "home") -> InlineKeyboardMarkup:
    key = "menu_home" if target == "home" else "menu_back"
    return InlineKeyboardMarkup([[btn(get_text(language, key), f"nav|{target}")]])


def home_only_keyboard(language: str) -> InlineKeyboardMarkup:
    return back_keyboard(language, "home")


def tools_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn(get_text(language, "menu_convert"), "nav|convert")],
        [btn(get_text(language, "menu_back"), "nav|home")],
    ])


def settings_keyboard(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [btn(get_text(language, "menu_change_lang"), "nav|language")],
        [btn(get_text(language, "menu_back"), "nav|home")],
    ])


def mode_row(language: str, mode: str) -> list[InlineKeyboardButton]:
    """Video / MP3 toggle used by YouTube search."""
    return [
        btn(("✅ " if mode == "video" else "") + get_text(language, "mode_video"), "sm|video"),
        btn(("✅ " if mode == "audio" else "") + get_text(language, "mode_audio"), "sm|audio"),
    ]


def ytsearch_keyboard(language: str, mode: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        mode_row(language, mode),
        [btn(get_text(language, "menu_back"), "nav|home")],
    ])


def quality_keyboard(user_id: int, language: str) -> InlineKeyboardMarkup:
    def t(key: str) -> str:
        return get_text(language, key)

    return InlineKeyboardMarkup([
        [btn(t("quality_best"), f"dl|{user_id}|best"), btn(t("quality_720"), f"dl|{user_id}|720p")],
        [btn(t("quality_480"), f"dl|{user_id}|480p"), btn(t("quality_360"), f"dl|{user_id}|360p")],
        [btn(t("audio_mp3"), f"dl|{user_id}|audio")],
        [btn(t("cancel"), "nav|home")],
    ])


def result_label(result: dict) -> str:
    """Button text for a YouTube result: title + duration (the duration is never cut)."""
    duration = format_duration(result.get("duration"))
    suffix = f" · {duration}" if duration else ""
    return _fit_label("🎬 ", str(result.get("title") or "Video"), suffix)


def youtube_results_keyboard(
    user_id: int, results: list[dict], language: str, mode: str
) -> InlineKeyboardMarkup:
    rows = [[btn(result_label(result), f"ytr|{user_id}|{index}")] for index, result in enumerate(results)]
    rows.append(mode_row(language, mode))
    rows.append([btn(get_text(language, "menu_home"), "nav|home")])
    return InlineKeyboardMarkup(rows)


def song_results_keyboard(user_id: int, results: list[dict], language: str) -> InlineKeyboardMarkup:
    rows = []
    for index, result in enumerate(results):
        title = str(result.get("title") or "Track")
        artist = str(result.get("uploader") or "")
        source = str(result.get("source") or "")
        duration = format_duration(result.get("duration"))
        base = " — ".join(part for part in (title, artist, source) if part)
        suffix = f" · {duration}" if duration else ""
        rows.append([btn(_fit_label("", base, suffix), f"song|{user_id}|{index}")])
    rows.append([btn(get_text(language, "menu_home"), "nav|home")])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# Small Telegram helpers
# --------------------------------------------------------------------------- #
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    """First URL in ``text`` with trailing punctuation removed; http:// is upgraded."""
    match = _URL_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,!?)]}»”\"'،؛؟")
    if url[:7].lower() == "http://":
        url = "https://" + url[7:]
    return url


async def safe_answer(query, text: str | None = None, show_alert: bool = False) -> None:
    try:
        await query.answer(text, show_alert=show_alert)
    except TelegramError:
        LOGGER.debug("Could not answer callback query", exc_info=True)


async def safe_edit(message, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Edit a status message; 'not modified', deleted or too-old messages are ignored."""
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramError:
        LOGGER.debug("Could not edit message", exc_info=True)


async def send_text(bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    return await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def reply(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> Message:
    return await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


def _spawn(coro) -> None:
    """Run a coroutine in the background without letting the task be garbage-collected."""
    task = asyncio.ensure_future(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _int_or_none(value: object) -> int | None:
    try:
        number = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def localized_media_error(language: str, error: BaseException) -> str:
    keys = {
        "youtube_challenge": "youtube_challenge",
        "rate_limited": "rate_limited",
        "access_denied": "access_denied",
        "configuration_error": "cookie_config_error",
        "restricted": "restricted_media",
        "unavailable": "video_unavailable",
        "live": "live_unsupported",
        "format_unavailable": "format_unavailable",
        "too_large": "size_error",
        "not_found": "song_not_found",
        "unsupported": "unsupported_link",
        "network": "network_error",
        "download_error": "error_download",
    }
    category = classify_media_error(error)
    return get_text(language, keys.get(category, "error_download"))


# --------------------------------------------------------------------------- #
# Screens / navigation
# --------------------------------------------------------------------------- #
SCREEN_AWAITING = {"download": "url", "ytsearch": "ytsearch", "song": "song"}


def screen_content(screen: str, language: str, context: ContextTypes.DEFAULT_TYPE):
    """Return (text, keyboard) for a menu screen, or None for an unknown screen."""
    mode = context.user_data.get("search_mode", "video")
    if screen == "home":
        return get_text(language, "home_text"), main_menu_keyboard(language)
    if screen == "download":
        return get_text(language, "download_text"), back_keyboard(language, "home")
    if screen == "ytsearch":
        return get_text(language, "ytsearch_text"), ytsearch_keyboard(language, mode)
    if screen == "song":
        return get_text(language, "song_text"), back_keyboard(language, "home")
    if screen == "tools":
        return get_text(language, "tools_text"), tools_keyboard(language)
    if screen == "convert":
        return get_text(language, "send_doc"), back_keyboard(language, "tools")
    if screen == "settings":
        return get_text(language, "settings_text"), settings_keyboard(language)
    if screen == "language":
        return get_text(language, "choose_language"), language_keyboard("settings", language, language)
    if screen == "help":
        return get_text(language, "help"), back_keyboard(language, "home")
    return None


async def show_screen(query, context: ContextTypes.DEFAULT_TYPE, text: str, markup) -> None:
    """Edit the menu message in place; fall back to a new message if editing is impossible."""
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        chat_id = query.message.chat.id if query.message else query.from_user.id
        await send_text(context.bot, chat_id, text, markup)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    context.user_data.pop("awaiting", None)
    language = await stored_language(user.id)
    if language:
        await reply(message, get_text(language, "welcome"), main_menu_keyboard(language))
    else:
        guessed = guess_language(user)
        await reply(message, get_text(guessed, "choose_language"), language_keyboard(language=guessed))


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    context.user_data.pop("awaiting", None)
    language = await user_language(user)
    await reply(message, get_text(language, "home_text"), main_menu_keyboard(language))


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if user and message:
        language = await user_language(user)
        await reply(message, get_text(language, "help"), back_keyboard(language, "home"))


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/search <name> searches YouTube right away; /search alone opens the search screen."""
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    language = await user_language(user)
    query_text = " ".join(context.args or []).strip()
    if query_text:
        context.user_data["awaiting"] = "ytsearch"
        await show_youtube_search(message, context, user, query_text[:250], language)
        return
    context.user_data["awaiting"] = "ytsearch"
    mode = context.user_data.get("search_mode", "video")
    await reply(message, get_text(language, "ytsearch_text"), ytsearch_keyboard(language, mode))


# --------------------------------------------------------------------------- #
# Callback buttons
# --------------------------------------------------------------------------- #
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    kind, _, rest = (query.data or "").partition("|")
    handlers = {
        "lang": on_language,
        "nav": on_navigation,
        "sm": on_search_mode,
        "ytr": on_youtube_result,
        "song": on_song_result,
        "dl": on_quality,
    }
    handler = handlers.get(kind)
    if handler is None:
        await safe_answer(query)
        return
    language = await user_language(user)
    await handler(context, query, user, language, rest)


def _chat_id(query, user) -> int:
    return query.message.chat.id if query.message else user.id


async def on_language(context, query, user, language, rest) -> None:
    if rest not in LANGUAGES:
        await safe_answer(query, "Invalid language", show_alert=True)
        return
    await asyncio.to_thread(store.set_language, user.id, rest)
    await safe_answer(query, get_text(rest, "language_saved"))
    await show_screen(query, context, get_text(rest, "welcome"), main_menu_keyboard(rest))


async def on_navigation(context, query, user, language, screen) -> None:
    if screen == "movies":
        # Telegram cannot grey-out an inline button, so the button stays visible and
        # answers with a modal popup (alert) instead of doing anything.
        await safe_answer(query, get_text(language, "coming_soon"), show_alert=True)
        return
    content = screen_content(screen, language, context)
    if content is None:
        await safe_answer(query)
        return
    await safe_answer(query)
    awaiting = SCREEN_AWAITING.get(screen)
    if awaiting:
        context.user_data["awaiting"] = awaiting
    else:
        context.user_data.pop("awaiting", None)
    if screen == "home":
        context.user_data.pop("pending_url", None)
    await show_screen(query, context, *content)


async def on_search_mode(context, query, user, language, mode) -> None:
    if mode not in {"video", "audio"}:
        await safe_answer(query)
        return
    context.user_data["search_mode"] = mode
    await safe_answer(query)
    results = context.user_data.get("yt_results")
    on_results_message = bool(
        results and query.message and context.user_data.get("yt_results_msg") == query.message.message_id
    )
    markup = (
        youtube_results_keyboard(user.id, results, language, mode)
        if on_results_message
        else ytsearch_keyboard(language, mode)
    )
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except TelegramError:
        LOGGER.debug("Search-mode keyboard unchanged", exc_info=True)


def _parse_owned_index(rest: str, user_id: int) -> tuple[int | None, bool]:
    """Parse '<user_id>|<index>'. Returns (index, belongs_to_user)."""
    parts = rest.split("|")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None, False
    return int(parts[1]), int(parts[0]) == user_id


async def on_youtube_result(context, query, user, language, rest) -> None:
    index, owned = _parse_owned_index(rest, user.id)
    if not owned or index is None:
        await safe_answer(query, get_text(language, "not_yours"), show_alert=True)
        return
    results = context.user_data.get("yt_results") or []
    if index >= len(results):
        await safe_answer(query, get_text(language, "expired"), show_alert=True)
        return
    url = str(results[index].get("webpage_url") or "")
    if not validate_media_url(url):
        await safe_answer(query, get_text(language, "expired"), show_alert=True)
        return
    if user.id in _ACTIVE_USERS:
        await safe_answer(query, get_text(language, "busy"), show_alert=True)
        return
    await safe_answer(query)
    mode = context.user_data.get("search_mode", "video")
    quality = "audio" if mode == "audio" else DEFAULT_SEARCH_QUALITY
    await perform_download(context, _chat_id(query, user), user.id, url, mode, quality, language)


async def on_song_result(context, query, user, language, rest) -> None:
    index, owned = _parse_owned_index(rest, user.id)
    if not owned or index is None:
        await safe_answer(query, get_text(language, "not_yours"), show_alert=True)
        return
    results = context.user_data.get("song_search_results") or []
    if index >= len(results):
        await safe_answer(query, get_text(language, "expired"), show_alert=True)
        return
    url = str(results[index].get("webpage_url") or "")
    if not validate_media_url(url):
        await safe_answer(query, get_text(language, "expired"), show_alert=True)
        return
    if user.id in _ACTIVE_USERS:
        await safe_answer(query, get_text(language, "busy"), show_alert=True)
        return
    context.user_data.pop("song_search_results", None)
    await safe_answer(query)
    await perform_download(context, _chat_id(query, user), user.id, url, "audio", "audio", language)


async def on_quality(context, query, user, language, rest) -> None:
    parts = rest.split("|")
    if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in VALID_QUALITIES:
        await safe_answer(query)
        return
    if int(parts[0]) != user.id:
        await safe_answer(query, get_text(language, "not_yours"), show_alert=True)
        return
    pending_url = context.user_data.get("pending_url")
    if not pending_url:
        await safe_answer(query, get_text(language, "expired"), show_alert=True)
        return
    if user.id in _ACTIVE_USERS:
        await safe_answer(query, get_text(language, "busy"), show_alert=True)
        return
    context.user_data.pop("pending_url", None)
    await safe_answer(query)
    quality = parts[1]
    mode = "audio" if quality == "audio" else "video"
    # Reuse the quality-picker message as the live status message.
    status = query.message if isinstance(query.message, Message) else None
    await perform_download(
        context, _chat_id(query, user), user.id, pending_url, mode, quality, language, status
    )


# --------------------------------------------------------------------------- #
# Text messages and searches
# --------------------------------------------------------------------------- #
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not message.text or not user:
        return
    language = await user_language(user)
    text = message.text.strip()
    awaiting = context.user_data.get("awaiting")

    url = extract_url(text)
    if url:
        if not validate_media_url(url):
            await reply(message, get_text(language, "bad_url"), home_only_keyboard(language))
            return
        if awaiting == "song":  # a direct link in "find a song" mode means: give me the MP3
            context.user_data.pop("awaiting", None)
            await perform_download(context, message.chat_id, user.id, url, "audio", "audio", language)
            return
        context.user_data["pending_url"] = url
        await reply(message, get_text(language, "choose_quality"), quality_keyboard(user.id, language))
        return

    if awaiting == "ytsearch":
        await show_youtube_search(message, context, user, text[:250], language)
    elif awaiting == "song":
        await show_song_search(message, context, user, text[:250], language)
    elif awaiting == "url":
        await reply(message, get_text(language, "ask_url"), back_keyboard(language, "home"))
    else:
        await reply(message, get_text(language, "hint"), main_menu_keyboard(language))


async def _run_search(function, query_text: str, limit: int) -> list[dict]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(function, query_text, limit), SEARCH_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise MediaTimeoutError("search timed out") from exc


async def show_youtube_search(message, context, user, query_text: str, language: str) -> None:
    status = await reply(message, get_text(language, "searching"))
    try:
        results = await _run_search(search_youtube, query_text, YOUTUBE_RESULT_LIMIT)
    except Exception as exc:
        LOGGER.warning("YouTube search failed (%s)", classify_media_error(exc), exc_info=True)
        await safe_edit(status, localized_media_error(language, exc), home_only_keyboard(language))
        return
    if not results:
        await safe_edit(status, get_text(language, "song_not_found"), home_only_keyboard(language))
        return
    mode = context.user_data.get("search_mode", "video")
    context.user_data["yt_results"] = results
    context.user_data["yt_results_msg"] = status.message_id
    await safe_edit(
        status,
        get_text(language, "results_title", query=html.escape(query_text[:80])),
        youtube_results_keyboard(user.id, results, language, mode),
    )


async def show_song_search(message, context, user, query_text: str, language: str) -> None:
    status = await reply(message, get_text(language, "searching"))
    try:
        results = await _run_search(search_songs, query_text, SONG_RESULT_LIMIT)
    except Exception as exc:
        LOGGER.warning("Song search failed (%s)", classify_media_error(exc), exc_info=True)
        await safe_edit(status, localized_media_error(language, exc), home_only_keyboard(language))
        return
    if not results:
        await safe_edit(status, get_text(language, "song_not_found"), home_only_keyboard(language))
        return
    context.user_data["song_search_results"] = results
    await safe_edit(status, get_text(language, "choose_song"), song_results_keyboard(user.id, results, language))


# --------------------------------------------------------------------------- #
# Downloading
# --------------------------------------------------------------------------- #
def _clear_directory(path: str) -> None:
    for item in Path(path).iterdir():
        try:
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        except OSError:
            LOGGER.debug("Could not remove %s", item, exc_info=True)


async def _download_with_fallbacks(
    url: str,
    temp_dir: str,
    mode: str,
    quality: str,
    progress_hook,
    last_step: list[int],
    status: Message,
    language: str,
):
    """Download, automatically retrying smaller (lower quality / MP3 bitrate) if too big."""
    if mode == "audio":
        attempts = [{"audio_bitrate": rate} for rate in audio_bitrate_ladder()]
    else:
        attempts = [{"quality": rung} for rung in fallback_qualities(quality)]
    last_error: MediaTooLargeError | None = None
    for index, options in enumerate(attempts):
        last_step[0] = -1
        try:
            return await asyncio.to_thread(
                download_media,
                url,
                temp_dir,
                mode,
                options.get("quality", quality),
                progress_hook,
                max_bytes=MAX_UPLOAD_BYTES,
                audio_bitrate=options.get("audio_bitrate"),
            )
        except MediaTooLargeError as exc:
            last_error = exc
            if index + 1 < len(attempts):
                await safe_edit(status, get_text(language, "retry_smaller"))
                _clear_directory(temp_dir)
    assert last_error is not None
    raise last_error


async def _send_result(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    status: Message,
    language: str,
    mode: str,
    media_path: Path,
    info: dict,
    thumbnail: Path | None,
) -> None:
    bot = context.bot
    await safe_edit(status, get_text(language, "download_done"))
    title = str(info.get("title") or "Downloaded media")[:128]
    artist = str(info.get("artist") or info.get("uploader") or info.get("channel") or "")[:64]
    suffix = media_path.suffix.lower()
    filename = safe_filename(title, suffix)
    timeouts = {
        "read_timeout": UPLOAD_TIMEOUT,
        "write_timeout": UPLOAD_TIMEOUT,
        "connect_timeout": 30,
        "pool_timeout": 30,
    }
    duration = _int_or_none(info.get("duration"))
    with media_path.open("rb") as handle:
        if mode == "audio":
            await bot.send_audio(
                chat_id=chat_id,
                audio=InputFile(handle, filename=filename),
                title=title,
                performer=artist or None,
                duration=duration,
                **timeouts,
            )
        elif suffix == ".mp4":
            await bot.send_video(
                chat_id=chat_id,
                video=InputFile(handle, filename=filename),
                caption=title[:1024],
                duration=duration,
                width=_int_or_none(info.get("width")),
                height=_int_or_none(info.get("height")),
                supports_streaming=True,
                **timeouts,
            )
        else:
            await bot.send_document(
                chat_id=chat_id,
                document=InputFile(handle, filename=filename),
                caption=title[:1024],
                **timeouts,
            )
    if mode == "audio" and thumbnail and thumbnail.is_file() and thumbnail.stat().st_size < 10 * 1024 * 1024:
        try:
            with thumbnail.open("rb") as image_file:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=InputFile(image_file, filename=thumbnail.name),
                    caption=(f"{title} — {artist}" if artist else title)[:1024],
                    **timeouts,
                )
        except TelegramError:  # the cover is a nice extra; never fail the download over it
            LOGGER.warning("Could not send the cover image", exc_info=True)
    await safe_edit(status, get_text(language, "sent_done"), home_only_keyboard(language))


async def perform_download(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    url: str,
    mode: str,
    quality: str,
    language: str,
    status_message: Message | None = None,
) -> None:
    """Queue, download and send media, keeping the user informed at every step."""
    if user_id in _ACTIVE_USERS:
        await send_text(context.bot, chat_id, get_text(language, "busy"))
        return
    _ACTIVE_USERS.add(user_id)
    status: Message | None = status_message
    try:
        first_key = "queued" if _DOWNLOAD_SLOTS.locked() else "downloading"
        first_text = get_text(language, first_key, percent="")
        if status is None:
            status = await send_text(context.bot, chat_id, first_text)
        else:
            await safe_edit(status, first_text)

        loop = asyncio.get_running_loop()
        last_step = [-1]
        progress_target = status

        def progress_hook(data: dict[str, Any]) -> None:
            if data.get("status") != "downloading":
                return
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            if not total:
                return
            percent = max(0, min(100, int(data.get("downloaded_bytes", 0) * 100 / total)))
            step = percent // 10
            if step > last_step[0]:
                last_step[0] = step
                text = get_text(language, "downloading", percent=f"{percent}%")
                try:
                    loop.call_soon_threadsafe(lambda: _spawn(safe_edit(progress_target, text)))
                except RuntimeError:  # event loop already closed
                    pass

        with tempfile.TemporaryDirectory(prefix="telegram-media-") as temp_dir:
            async with _DOWNLOAD_SLOTS:  # the slot is released before the (slow) upload
                await safe_edit(status, get_text(language, "downloading", percent=""))
                media_path, info, thumbnail = await _download_with_fallbacks(
                    url, temp_dir, mode, quality, progress_hook, last_step, status, language
                )
            try:
                await _send_result(context, chat_id, status, language, mode, media_path, info, thumbnail)
            except TelegramError:
                LOGGER.warning("Upload to Telegram failed", exc_info=True)
                await safe_edit(status, get_text(language, "upload_error"), home_only_keyboard(language))
    except Exception as exc:  # never let one failing link take the bot down
        LOGGER.warning("Media download failed (%s)", classify_media_error(exc), exc_info=True)
        error_text = localized_media_error(language, exc)
        try:
            if status is not None:
                await status.edit_text(
                    error_text, parse_mode=ParseMode.HTML, reply_markup=home_only_keyboard(language)
                )
            else:
                await send_text(context.bot, chat_id, error_text, home_only_keyboard(language))
        except TelegramError:
            try:
                await send_text(context.bot, chat_id, error_text, home_only_keyboard(language))
            except TelegramError:
                LOGGER.error("Could not report the error to the user", exc_info=True)
    finally:
        _ACTIVE_USERS.discard(user_id)


# --------------------------------------------------------------------------- #
# Document conversion
# --------------------------------------------------------------------------- #
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    document = message.document if message else None
    if not message or not document or not user:
        return
    language = await user_language(user)
    filename = document.file_name or "document"
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        await reply(message, get_text(language, "unsupported_doc"), home_only_keyboard(language))
        return
    if document.file_size and document.file_size > MAX_INCOMING_BYTES:
        await reply(message, get_text(language, "file_too_big_in"), home_only_keyboard(language))
        return

    status = await reply(message, get_text(language, "converting"))
    try:
        with tempfile.TemporaryDirectory(prefix="office-to-pdf-") as temp_dir:
            root = Path(temp_dir)
            source_path = root / Path(filename).name
            telegram_file = await document.get_file()
            await telegram_file.download_to_drive(custom_path=source_path)
            async with _CONVERT_SLOTS:
                pdf_path = await asyncio.to_thread(convert_office_to_pdf, source_path, root)
            if pdf_path.stat().st_size > MAX_UPLOAD_BYTES:
                await safe_edit(status, get_text(language, "size_error"), home_only_keyboard(language))
                return
            with pdf_path.open("rb") as pdf_file:
                await message.reply_document(
                    document=InputFile(pdf_file, filename=pdf_path.name),
                    caption=get_text(language, "pdf_done"),
                    write_timeout=UPLOAD_TIMEOUT,
                    read_timeout=UPLOAD_TIMEOUT,
                )
            await safe_edit(status, get_text(language, "pdf_done"), home_only_keyboard(language))
    except Exception as exc:
        LOGGER.warning("Document conversion failed: %s", exc, exc_info=True)
        await safe_edit(status, get_text(language, "convert_error"), home_only_keyboard(language))


# --------------------------------------------------------------------------- #
# Application wiring
# --------------------------------------------------------------------------- #
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Unhandled update error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_user:
        language = await user_language(update.effective_user)
        try:
            if update.callback_query:
                await safe_answer(update.callback_query, get_text(language, "generic_error"), show_alert=True)
            elif update.effective_message:
                await reply(update.effective_message, get_text(language, "generic_error"))
        except TelegramError:
            pass


def build_application(token: str) -> Application:
    application = (
        ApplicationBuilder()
        .token(token)
        .concurrent_updates(8)
        .connect_timeout(20)
        .read_timeout(60)
        .write_timeout(120)
        .pool_timeout(30)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("help", handle_help))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    return application


async def register_commands(bot) -> None:
    """Publish the "/" command menu in every supported language (best effort)."""
    try:
        default = COMMAND_DESCRIPTIONS["en"]
        await bot.set_my_commands([BotCommand(name, text) for name, text in default.items()])
        for code, commands in COMMAND_DESCRIPTIONS.items():
            await bot.set_my_commands(
                [BotCommand(name, text) for name, text in commands.items()], language_code=code
            )
    except TelegramError:
        LOGGER.warning("Could not register bot commands", exc_info=True)


telegram_app: Application | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")
    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET environment variable is required")
    player_clients = os.getenv("YOUTUBE_PLAYER_CLIENTS", "").strip()
    pot_provider_configured = bool(os.getenv("YOUTUBE_POT_BASE_URL", "").strip())
    LOGGER.info(
        "YouTube downloader configuration: player_clients=%s cookies_configured=%s "
        "po_token_provider_configured=%s",
        player_clients or ("mweb (PO-token provider)" if pot_provider_configured else "yt-dlp automatic"),
        bool(os.getenv("YOUTUBE_COOKIES_FILE", "").strip()),
        pot_provider_configured,
    )
    telegram_app = build_application(BOT_TOKEN)
    await telegram_app.initialize()
    await telegram_app.start()
    webhook_url = os.getenv("WEBHOOK_URL", "").strip()
    if not webhook_url:
        hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
        if not hostname:
            raise RuntimeError("Set WEBHOOK_URL or deploy with RENDER_EXTERNAL_HOSTNAME")
        webhook_url = f"https://{hostname}/telegram"
    if not webhook_url.startswith("https://"):
        raise RuntimeError("WEBHOOK_URL must use HTTPS")
    await telegram_app.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )
    await register_commands(telegram_app.bot)
    LOGGER.info("Telegram webhook registered")
    try:
        yield
    finally:
        if telegram_app.running:
            await telegram_app.stop()
        await telegram_app.shutdown()
        telegram_app = None


api = FastAPI(title="Hormuz Telegram media bot", lifespan=lifespan)


@api.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@api.post("/telegram")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    if not WEBHOOK_SECRET or not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    if telegram_app is None:
        raise HTTPException(status_code=503, detail="Bot is starting")
    try:
        payload = await request.json()
        update = Update.de_json(payload, telegram_app.bot)
        await telegram_app.update_queue.put(update)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Telegram update") from exc
    return JSONResponse({"ok": True})
