"""Multilingual Telegram media bot served through a FastAPI webhook."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
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
from downloader import classify_media_error, download_media, search_songs, validate_media_url
from locales import LANGUAGES, get_text

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("telegram_media_bot")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "/tmp/telegram_bot.sqlite3"))
MAX_UPLOAD_BYTES = 49 * 1024 * 1024
MAX_INCOMING_BYTES = 19 * 1024 * 1024
_DOWNLOAD_SLOTS = asyncio.Semaphore(2)


class UserStore:
    """Small SQLite store for language preferences."""

    def __init__(self, path: Path):
        self.path = path
        self.database_url = os.getenv("DATABASE_URL", "").strip()
        if self.database_url.startswith("postgres://"):
            self.database_url = "postgresql://" + self.database_url[len("postgres://"):]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS user_preferences "
                "(user_id BIGINT PRIMARY KEY, language TEXT NOT NULL)"
            )

    def _connect(self):
        if self.database_url:
            import psycopg

            return psycopg.connect(self.database_url)
        return sqlite3.connect(self.path)

    @property
    def _placeholder(self) -> str:
        return "%s" if self.database_url else "?"

    def get_language(self, user_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT language FROM user_preferences WHERE user_id = {self._placeholder}",
                (user_id,),
            ).fetchone()
        return row[0] if row and row[0] in LANGUAGES else None

    def set_language(self, user_id: int, language: str) -> None:
        if language not in LANGUAGES:
            raise ValueError("Unsupported language")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO user_preferences(user_id, language) VALUES(" + self._placeholder + ", " + self._placeholder + ") "
                "ON CONFLICT(user_id) DO UPDATE SET language = excluded.language",
                (user_id, language),
            )


store = UserStore(DATABASE_PATH)


def language_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=f"lang|{code}")] for label, code in LANGUAGES.values()]
    )


def main_menu_keyboard(language: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(get_text(language, "menu_download"), callback_data="menu|download")],
        [InlineKeyboardButton(get_text(language, "menu_audio_search"), callback_data="menu|audio")],
        [InlineKeyboardButton(get_text(language, "menu_convert"), callback_data="menu|convert")],
        [InlineKeyboardButton(get_text(language, "menu_change_lang"), callback_data="menu|language")],
        [InlineKeyboardButton(get_text(language, "menu_help"), callback_data="menu|help")],
    ]
    return InlineKeyboardMarkup(rows)


def quality_keyboard(user_id: int, language: str) -> InlineKeyboardMarkup:
    values = [
        ("best", get_text(language, "quality_best")),
        ("720p", get_text(language, "quality_720")),
        ("480p", get_text(language, "quality_480")),
        ("360p", get_text(language, "quality_360")),
        ("audio", get_text(language, "audio_mp3")),
    ]
    rows = [
        [InlineKeyboardButton(label, callback_data=f"dl|{user_id}|{value}")]
        for value, label in values
    ]
    return InlineKeyboardMarkup(rows)


def song_results_keyboard(user_id: int, results: list[dict], language: str) -> InlineKeyboardMarkup:
    rows = []
    for index, result in enumerate(results):
        title = str(result.get("title") or "Track")
        artist = str(result.get("uploader") or "")
        source = str(result.get("source") or "")
        label = " — ".join(part for part in (title, artist, source) if part)[:60]
        rows.append([InlineKeyboardButton(label, callback_data=f"song|{user_id}|{index}")])
    return InlineKeyboardMarkup(rows)


def localized_media_error(language: str, error: BaseException) -> str:
    keys = {
        "youtube_challenge": "youtube_challenge",
        "rate_limited": "rate_limited",
        "access_denied": "access_denied",
        "configuration_error": "cookie_config_error",
        "restricted": "restricted_media",
        "not_found": "song_not_found",
        "unsupported": "unsupported_link",
        "network": "network_error",
        "download_error": "error_download",
    }
    category = classify_media_error(error)
    return get_text(language, keys.get(category, "error_download"))


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    language = store.get_language(user.id)
    if language:
        await message.reply_text(
            get_text(language, "welcome"), reply_markup=main_menu_keyboard(language)
        )
    else:
        await message.reply_text(
            get_text("en", "choose_language"), reply_markup=language_keyboard()
        )


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    data = query.data or ""
    if data.startswith("lang|"):
        language = data.split("|", 1)[1]
        if language not in LANGUAGES:
            await query.answer("Invalid language", show_alert=True)
            return
        store.set_language(user.id, language)
        await query.answer(get_text(language, "language_saved"))
        await query.edit_message_text(
            get_text(language, "welcome"), reply_markup=main_menu_keyboard(language)
        )
        return

    if data.startswith("menu|"):
        language = store.get_language(user.id) or "en"
        action = data.split("|", 1)[1]
        await query.answer()
        if action == "language":
            await query.edit_message_text(
                get_text(language, "choose_language"), reply_markup=language_keyboard()
            )
        elif action == "download":
            context.user_data["awaiting_url"] = True
            await query.message.reply_text(get_text(language, "ask_url"))
        elif action == "audio":
            context.user_data["awaiting_song"] = True
            await query.message.reply_text(get_text(language, "ask_song"))
        elif action == "convert":
            await query.message.reply_text(get_text(language, "send_doc"))
        elif action == "help":
            await query.message.reply_text(get_text(language, "help"))
        return

    if data.startswith("song|"):
        parts = data.split("|")
        if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) != user.id or not parts[2].isdigit():
            await query.answer("This result belongs to another user.", show_alert=True)
            return
        results = context.user_data.get("song_search_results", [])
        index = int(parts[2])
        if index >= len(results):
            await query.answer("Search expired. Please search again.", show_alert=True)
            return
        result = results[index]
        media_url = str(result.get("webpage_url") or "")
        if not validate_media_url(media_url):
            await query.answer("Unsupported result. Please search again.", show_alert=True)
            return
        context.user_data.pop("song_search_results", None)
        language = store.get_language(user.id) or "en"
        await query.answer()
        await perform_download(update, context, media_url, "audio", "audio", language)
        return

    if data.startswith("dl|"):
        parts = data.split("|")
        if len(parts) != 3 or not parts[1].isdigit() or int(parts[1]) != user.id:
            await query.answer("This download button belongs to another user.", show_alert=True)
            return
        pending_url = context.user_data.pop("pending_url", None)
        language = store.get_language(user.id) or "en"
        if not pending_url:
            await query.answer("Please send the link again.", show_alert=True)
            return
        await query.answer()
        quality = parts[2]
        mode = "audio" if quality == "audio" else "video"
        await perform_download(update, context, pending_url, mode, quality, language)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not message.text or not user:
        return
    language = store.get_language(user.id) or "en"
    text = message.text.strip()

    if context.user_data.pop("awaiting_song", False):
        match = re.search(r"https?://\S+", text)
        if match:
            url = match.group(0).rstrip(".,!?)]}")
            if not validate_media_url(url):
                await message.reply_text(get_text(language, "bad_url"))
                return
            await perform_download(update, context, url, "audio", "audio", language)
        else:
            await show_song_search(message, context, text[:250], language)
        return

    match = re.search(r"https?://\S+", text)
    if not match:
        await message.reply_text(get_text(language, "help"))
        return
    url = match.group(0).rstrip(".,!?)]}")
    if not validate_media_url(url):
        await message.reply_text(get_text(language, "bad_url"))
        return
    context.user_data["pending_url"] = url
    context.user_data["awaiting_url"] = False
    await message.reply_text(
        get_text(language, "choose_quality"),
        reply_markup=quality_keyboard(user.id, language),
    )


async def show_song_search(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
    language: str,
) -> None:
    status = await message.reply_text(get_text(language, "searching"))
    try:
        results = await asyncio.to_thread(search_songs, query, 5)
    except Exception as exc:
        LOGGER.warning("Song search failed (%s)", classify_media_error(exc), exc_info=True)
        await status.edit_text(localized_media_error(language, exc))
        return
    if not results:
        await status.edit_text(get_text(language, "song_not_found"))
        return
    context.user_data["song_search_results"] = results
    await status.edit_text(
        get_text(language, "choose_song"),
        reply_markup=song_results_keyboard(message.from_user.id, results, language),
    )


async def _safe_progress_edit(message, text: str) -> None:
    try:
        await message.edit_text(text)
    except (BadRequest, TelegramError):
        pass


async def perform_download(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query: str,
    mode: str,
    quality: str,
    language: str,
) -> None:
    message = update.effective_message
    if not message:
        return
    status_message = await message.reply_text(get_text(language, "downloading", percent=""))
    loop = asyncio.get_running_loop()
    last_step = [-1]

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
            label = f"{percent}%"
            loop.call_soon_threadsafe(
                lambda current=label: asyncio.create_task(
                    _safe_progress_edit(
                        status_message,
                        get_text(language, "downloading", percent=current),
                    )
                )
            )

    try:
        async with _DOWNLOAD_SLOTS:
            with tempfile.TemporaryDirectory(prefix="telegram-media-") as temp_dir:
                media_path, info, thumbnail = await asyncio.to_thread(
                    download_media, query, temp_dir, mode, quality, progress_hook
                )
                if media_path.stat().st_size > MAX_UPLOAD_BYTES:
                    await status_message.edit_text(get_text(language, "size_error"))
                    return
                await status_message.edit_text(get_text(language, "download_done"))
                title = str(info.get("title") or "Downloaded media")[:128]
                artist = str(info.get("artist") or info.get("uploader") or info.get("channel") or "")[:64]
                if mode == "audio":
                    with media_path.open("rb") as audio_file:
                        await message.reply_audio(
                            audio=InputFile(audio_file, filename=media_path.name),
                            title=title,
                            performer=artist or None,
                        )
                    if thumbnail and thumbnail.is_file() and thumbnail.stat().st_size < 10 * 1024 * 1024:
                        with thumbnail.open("rb") as image_file:
                            await message.reply_photo(
                                photo=InputFile(image_file, filename=thumbnail.name),
                                caption=(f"{title} — {artist}" if artist else title)[:1024],
                            )
                elif media_path.suffix.lower() == ".mp4":
                    with media_path.open("rb") as video_file:
                        await message.reply_video(
                            video=InputFile(video_file, filename=media_path.name),
                            caption=title[:1024],
                            supports_streaming=True,
                        )
                else:
                    with media_path.open("rb") as document_file:
                        await message.reply_document(
                            document=InputFile(document_file, filename=media_path.name),
                            caption=title[:1024],
                        )
    except Exception as exc:
        category = classify_media_error(exc)
        LOGGER.warning("Media download failed (%s)", category, exc_info=True)
        error_message = localized_media_error(language, exc)
        try:
            await status_message.edit_text(error_message)
        except TelegramError:
            await message.reply_text(error_message)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    document = message.document if message else None
    if not message or not document or not user:
        return
    language = store.get_language(user.id) or "en"
    filename = document.file_name or "document"
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        await message.reply_text(get_text(language, "unsupported_doc"))
        return
    if document.file_size and document.file_size > MAX_INCOMING_BYTES:
        await message.reply_text(get_text(language, "unsupported_doc"))
        return

    status = await message.reply_text(get_text(language, "converting"))
    try:
        with tempfile.TemporaryDirectory(prefix="office-to-pdf-") as temp_dir:
            root = Path(temp_dir)
            source_path = root / Path(filename).name
            telegram_file = await document.get_file()
            await telegram_file.download_to_drive(custom_path=source_path)
            pdf_path = await asyncio.to_thread(convert_office_to_pdf, source_path, root)
            if pdf_path.stat().st_size > MAX_UPLOAD_BYTES:
                await status.edit_text(get_text(language, "size_error"))
                return
            await status.edit_text(get_text(language, "pdf_done"))
            with pdf_path.open("rb") as pdf_file:
                await message.reply_document(
                    document=InputFile(pdf_file, filename=pdf_path.name),
                    caption=get_text(language, "pdf_done"),
                )
    except Exception as exc:
        LOGGER.warning("Document conversion failed: %s", exc, exc_info=True)
        try:
            await status.edit_text(get_text(language, "convert_error"))
        except TelegramError:
            await message.reply_text(get_text(language, "convert_error"))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    LOGGER.error("Unhandled update error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message and update.effective_user:
        language = store.get_language(update.effective_user.id) or "en"
        try:
            await update.effective_message.reply_text(get_text(language, "generic_error"))
        except TelegramError:
            pass


def build_application(token: str) -> Application:
    application = ApplicationBuilder().token(token).concurrent_updates(8).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", handle_help))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_error_handler(error_handler)
    return application


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and update.effective_message:
        language = store.get_language(user.id) or "en"
        await update.effective_message.reply_text(get_text(language, "help"))


telegram_app: Application | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is required")
    if not WEBHOOK_SECRET:
        raise RuntimeError("WEBHOOK_SECRET environment variable is required")
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
    LOGGER.info("Telegram webhook registered")
    try:
        yield
    finally:
        if telegram_app.running:
            await telegram_app.stop()
        await telegram_app.shutdown()
        telegram_app = None


api = FastAPI(title="Telegram media bot", lifespan=lifespan)


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
