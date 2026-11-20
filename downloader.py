"""yt-dlp helpers for the Hormuz bot.

Every function in this module is blocking (network / ffmpeg work), so the bot
always calls them through ``asyncio.to_thread``.

Design goals
------------
* Stay inside Telegram's 50 MB bot upload limit *before* wasting time on a huge
  download (size-aware format selectors + early abort from the progress hook).
* Prefer H.264/AAC MP4 so videos play inline in Telegram.
* Never hang: hard time limit, retries, and typed errors that the bot can turn
  into friendly localized messages.
* Everything environment-specific (cookies, proxy, player clients, PO-token
  provider, JS runtime components) is optional and read from environment
  variables, so no code change is needed to tune a deployment.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_DOMAINS = (
    "youtube.com", "youtube-nocookie.com", "youtu.be", "tiktok.com",
    "instagram.com", "facebook.com", "fb.watch", "twitter.com", "x.com", "soundcloud.com",
)
QUALITY_HEIGHTS = {"720p": 720, "480p": 480, "360p": 360}
# When a video is still too large for Telegram, retry with these lower rungs.
QUALITY_FALLBACKS = {
    "best": ("480p", "360p"),
    "720p": ("480p", "360p"),
    "480p": ("360p",),
    "360p": (),
}
AUDIO_BITRATES = (192, 128, 64)  # kbps rungs for MP3 (lower rung = longer tracks fit)

MAX_MEDIA_BYTES = 49 * 1024 * 1024   # Telegram bots may upload up to 50 MB
VIDEO_SIZE_SHARE = 0.85              # video stream budget; the rest is left for audio
DEFAULT_DOWNLOAD_TIMEOUT = 900       # seconds
LIVE_STATES = {"is_live", "is_upcoming"}
THUMBNAIL_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
PARTIAL_SUFFIXES = {".part", ".ytdl", ".temp", ".json"}
COOKIE_HEADERS = {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class MediaDownloadError(Exception):
    """Base class for errors raised by this module (carries a stable category)."""

    category = "download_error"


class MediaTooLargeError(MediaDownloadError):
    category = "too_large"


class MediaTimeoutError(MediaDownloadError):
    category = "network"


class LiveStreamError(MediaDownloadError):
    category = "live"


class MediaNotFoundError(MediaDownloadError):
    category = "not_found"


def classify_media_error(error: BaseException) -> str:
    """Map yt-dlp/provider errors to stable, user-safe categories."""
    if isinstance(error, MediaDownloadError):
        return error.category
    message = str(error).casefold()
    if "youtube_cookies_file" in message or "cookie file must use" in message:
        return "configuration_error"
    # Age gates also say "sign in to confirm", so test them before the bot challenge.
    if "confirm your age" in message or "age-restricted" in message or "age restricted" in message:
        return "restricted"
    if (
        "sign in to confirm" in message
        or "not a bot" in message
        or "confirm you're not" in message
        or "confirm you’re not" in message
    ):
        return "youtube_challenge"
    if re.search(r"\b429\b", message) or "too many requests" in message or "rate limit" in message:
        return "rate_limited"
    if "requested format is not available" in message:
        return "format_unavailable"
    if "live event will begin" in message or "premieres in" in message or "is live" in message:
        return "live"
    if re.search(r"\b403\b", message) or "forbidden" in message:
        return "access_denied"
    if any(term in message for term in ("private video", "login required", "members-only", "members only")):
        return "restricted"
    if any(
        term in message
        for term in (
            "video unavailable", "this video is not available", "no longer available",
            "has been removed", "content isn't available", "content isn’t available",
            "account associated with this video has been terminated",
        )
    ):
        return "unavailable"
    if any(term in message for term in ("no video results", "no suitable result", "no media result")):
        return "not_found"
    if "unsupported url" in message or "no suitable extractor" in message:
        return "unsupported"
    if any(term in message for term in ("timed out", "timeout", "connection reset", "temporary failure", "http error 5")):
        return "network"
    return "download_error"


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #
def validate_media_url(url: str) -> bool:
    """Accept only HTTPS URLs hosted on supported video platforms."""
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS)
    except (ValueError, TypeError, AttributeError):
        return False


def format_duration(seconds: object) -> str:
    """Return 3:05 / 1:02:03 for a duration in seconds ('' when unknown)."""
    try:
        total = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def safe_filename(title: str, suffix: str, fallback: str = "media", limit: int = 80) -> str:
    """Build a filesystem- and Telegram-safe file name from a media title."""
    cleaned = unicodedata.normalize("NFKC", title or "")
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned[:limit].strip(" .") or fallback
    return cleaned + suffix


def fallback_qualities(quality: str) -> list[str]:
    """The requested quality followed by the smaller ones to try if it is too big."""
    return [quality, *QUALITY_FALLBACKS.get(quality, ())]


def audio_bitrate_ladder(start: int | None = None) -> list[str]:
    """MP3 bitrates to try, highest first (e.g. ['192', '128', '64'])."""
    start = start or _env_int("MP3_BITRATE", AUDIO_BITRATES[0])
    rungs = [start] + [rate for rate in AUDIO_BITRATES if rate < start]
    return [str(rate) for rate in dict.fromkeys(rungs)]


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_list(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def format_selector(quality: str, mode: str, max_bytes: int = MAX_MEDIA_BYTES) -> str:
    """Create a yt-dlp format selector for the requested output.

    For video the selector is a ladder of alternatives (``a/b/c``): yt-dlp uses the
    first alternative that matches.  Early rungs prefer H.264 + AAC (plays inline
    in Telegram) and cap the video stream at ``max_bytes * 0.85`` so that the final
    merged file fits Telegram's limit.  ``[filesize<?N]`` lets formats with an
    unknown size through, and the last rungs drop the constraints so *something*
    is always selected when the site offers any format at all.
    """
    if mode == "audio":
        return "bestaudio/best"
    if mode != "video":
        raise ValueError("mode must be 'video' or 'audio'")
    if quality == "best":
        height = ""
    else:
        limit = QUALITY_HEIGHTS.get(quality)
        if limit is None:
            raise ValueError("unsupported quality")
        height = f"[height<={limit}]"
    size = f"[filesize<?{int(max_bytes * VIDEO_SIZE_SHARE)}]"
    return "/".join(
        (
            f"bv*{height}[vcodec^=avc1]{size}+ba[ext=m4a]",
            f"b{height}[ext=mp4]{size}",
            f"bv*{height}{size}+ba",
            f"b{height}{size}",
            f"bv*{height}+ba",
            f"b{height}",
            "b",
        )
    )


# --------------------------------------------------------------------------- #
# yt-dlp configuration
# --------------------------------------------------------------------------- #
def _prepare_cookie_file(scratch_dir: Path | None) -> str | None:
    """Validate YOUTUBE_COOKIES_FILE and return a writable copy of it.

    yt-dlp rewrites the cookie jar when it finishes.  Render mounts Secret Files
    read-only, so we hand yt-dlp a private copy that lives in the per-download
    scratch directory (deleted automatically afterwards).
    """
    cookie_path = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
    if not cookie_path:
        return None
    path = Path(cookie_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError("YOUTUBE_COOKIES_FILE points to a missing file")
    with path.open("r", encoding="utf-8", errors="replace") as cookie_file:
        header = cookie_file.readline().strip()
    if header not in COOKIE_HEADERS:
        raise ValueError("Cookie file must use Netscape/Mozilla cookies.txt format")
    if scratch_dir is None:
        return str(path)
    scratch_dir.mkdir(parents=True, exist_ok=True)
    private_copy = scratch_dir / "cookies.txt"
    shutil.copyfile(path, private_copy)
    try:
        private_copy.chmod(0o600)
    except OSError:
        pass
    return str(private_copy)


def ytdlp_base_options(scratch_dir: str | Path | None = None, use_cookies: bool = True) -> dict:
    """Shared retry / JS-runtime options plus optional deployment tuning.

    Optional environment variables (all unset by default):
      YOUTUBE_COOKIES_FILE      Netscape cookies.txt from an account you own.
      YTDLP_PROXY               Proxy URL (http://, https://, socks5://) for all requests.
      YOUTUBE_PLAYER_CLIENTS    Comma list for yt-dlp's ``player_client`` (e.g. ``mweb,tv``).
      YOUTUBE_POT_BASE_URL      URL of a bgutil PO-token provider server (plugin required).
      YTDLP_REMOTE_COMPONENTS   Comma list such as ``ejs:github`` (only needed when the
                                ``yt-dlp-ejs`` package is not installed).
    """
    options: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        # YouTube needs an external JS runtime (Deno) + the yt-dlp-ejs solver scripts.
        "js_runtimes": {"deno": {}},
    }
    remote_components = _env_list("YTDLP_REMOTE_COMPONENTS")
    if remote_components:
        options["remote_components"] = remote_components
    proxy = os.getenv("YTDLP_PROXY", "").strip()
    if proxy:
        options["proxy"] = proxy

    extractor_args: dict[str, dict[str, list[str]]] = {}
    clients = _env_list("YOUTUBE_PLAYER_CLIENTS")
    if not clients:
        # Android and Android VR are valid fallback clients for guest/IP-blocked hosts.
        # An explicit YOUTUBE_PLAYER_CLIENTS value replaces this fallback entirely.
        clients = ["android", "android_vr", "web_safari"]
    pot_url = os.getenv("YOUTUBE_POT_BASE_URL", "").strip()
    if clients:
        extractor_args["youtube"] = {"player_client": clients}
    if pot_url:
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [pot_url]}
    if extractor_args:
        options["extractor_args"] = extractor_args

    if use_cookies:
        cookie_file = _prepare_cookie_file(Path(scratch_dir) if scratch_dir else None)
        if cookie_file:
            options["cookiefile"] = cookie_file
    return options


def _flat_search_options() -> dict:
    """Fast search: titles/durations only, no per-result extraction."""
    options = ytdlp_base_options(use_cookies=False)
    options.update({"skip_download": True, "extract_flat": "in_playlist"})
    return options


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def _normalize_words(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[\w]+", normalized, flags=re.UNICODE))


def _relevance_score(query: str, title: str, uploader: str = "") -> float:
    query_text = _normalize_words(query)
    title_text = _normalize_words(f"{uploader} {title}")
    if not query_text or not title_text:
        return 0.0
    query_words = set(query_text.split())
    title_words = set(title_text.split())
    overlap = len(query_words & title_words)
    recall = overlap / max(1, len(query_words))
    precision = overlap / max(1, len(title_words))
    similarity = difflib.SequenceMatcher(None, query_text, title_text).ratio()
    phrase_bonus = 0.35 if query_text in title_text else 0.0
    return min(1.35, recall * 0.55 + precision * 0.15 + similarity * 0.30 + phrase_bonus)


def rank_search_results(query: str, results: list[dict], limit: int = 5) -> list[dict]:
    """Rank search results against title/artist and remove duplicate URLs."""
    if limit < 1:
        return []
    ranked: list[tuple[float, int, dict]] = []
    seen: set[str] = set()
    for index, result in enumerate(results):
        title = str(result.get("title") or "").strip()
        url = str(result.get("webpage_url") or "").strip()
        if not title or not validate_media_url(url) or url in seen:
            continue
        seen.add(url)
        uploader = str(result.get("uploader") or result.get("artist") or "")
        ranked.append((_relevance_score(query, title, uploader), index, dict(result)))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [result for _, _, result in ranked[:limit]]


def _entry_to_result(entry: object, source: str) -> dict | None:
    """Convert one flat search entry into the dict the bot works with."""
    if not isinstance(entry, dict):
        return None
    if entry.get("is_live") or entry.get("live_status") in LIVE_STATES:
        return None
    title = str(entry.get("title") or "").strip()
    url = str(entry.get("webpage_url") or "").strip()
    if not url:
        candidate = str(entry.get("url") or "").strip()
        if candidate.startswith("https://"):
            url = candidate
        elif source == "YouTube" and entry.get("id"):
            url = f"https://www.youtube.com/watch?v={entry['id']}"
    if not title or not url or not validate_media_url(url):
        return None
    duration = entry.get("duration")
    return {
        "title": title,
        "webpage_url": url,
        "uploader": str(entry.get("uploader") or entry.get("channel") or entry.get("artist") or ""),
        "duration": int(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
        "thumbnail": entry.get("thumbnail"),
        "source": source,
    }


def _entries(info: object) -> list:
    return list((info or {}).get("entries") or []) if isinstance(info, dict) else []


def search_youtube(query: str, limit: int = 8) -> list[dict]:
    """Search YouTube and return up to ``limit`` playable videos in YouTube's order.

    Each result has ``title``, ``webpage_url``, ``uploader``, ``duration`` (seconds
    or None), ``thumbnail`` and ``source``.  Live streams are skipped.
    """
    import yt_dlp

    query = query.strip()[:250]
    if not query:
        return []
    limit = max(1, min(int(limit), 12))
    fetch = min(limit + 4, 16)  # ask for a few extra: some entries get filtered out
    with yt_dlp.YoutubeDL(_flat_search_options()) as ydl:
        info = ydl.extract_info(f"ytsearch{fetch}:{query}", download=False)
    results: list[dict] = []
    seen: set[str] = set()
    for entry in _entries(info):
        result = _entry_to_result(entry, "YouTube")
        if result and result["webpage_url"] not in seen:
            seen.add(result["webpage_url"])
            results.append(result)
    return results[:limit]


def search_songs(query: str, limit: int = 5) -> list[dict]:
    """Search YouTube and SoundCloud, rank candidates, and let users choose."""
    import yt_dlp

    query = query.strip()[:250]
    if not query:
        return []
    limit = max(1, min(int(limit), 10))
    found: list[dict] = []
    failures: list[Exception] = []
    with yt_dlp.YoutubeDL(_flat_search_options()) as ydl:
        for prefix, source in (("ytsearch", "YouTube"), ("scsearch", "SoundCloud")):
            try:
                info = ydl.extract_info(f"{prefix}{limit}:{query}", download=False)
            except Exception as exc:  # one provider failing must not kill the search
                failures.append(exc)
                continue
            for entry in _entries(info):
                result = _entry_to_result(entry, source)
                if result:
                    found.append(result)
    ranked = rank_search_results(query, found, limit=limit)
    if ranked:
        return ranked
    if failures:
        raise failures[0]
    return []


def inspect_media(query: str) -> dict:
    """Read metadata without downloading. Query may be a URL or yt-dlp search."""
    import yt_dlp

    options = ytdlp_base_options()
    options["skip_download"] = True
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(query, download=False)
    if info and "entries" in info:
        info = next((entry for entry in info["entries"] if entry), None)
    if not info:
        raise MediaNotFoundError("No media result found")
    return info


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def _reject_live(info: dict, *, incomplete: bool = False) -> str | None:
    if info.get("is_live") or info.get("live_status") in LIVE_STATES:
        return "live streams are not supported"
    return None


def _locate_output(destination: Path, info: dict, mode: str) -> Path:
    """Find the final media file produced by yt-dlp (after merging / MP3 conversion)."""
    for item in reversed(info.get("requested_downloads") or []):
        path = Path(str(item.get("filepath") or ""))
        if path.is_file() and path.suffix.lower() not in THUMBNAIL_SUFFIXES | PARTIAL_SUFFIXES:
            return path
    files = [p for p in destination.iterdir() if p.is_file()]
    if mode == "audio":
        media = [p for p in files if p.suffix.lower() == ".mp3"]
    else:
        media = [p for p in files if p.suffix.lower() not in THUMBNAIL_SUFFIXES | PARTIAL_SUFFIXES | {".txt"}]
    if not media:
        raise FileNotFoundError("yt-dlp completed without producing an output file")
    return max(media, key=lambda path: path.stat().st_mtime)


def download_media(
    query: str,
    output_dir: str | Path,
    mode: str = "video",
    quality: str = "best",
    progress_hook=None,
    *,
    max_bytes: int = MAX_MEDIA_BYTES,
    max_seconds: int | None = None,
    audio_bitrate: str | int | None = None,
) -> tuple[Path, dict, Path | None]:
    """Download media, returning (media_path, metadata, thumbnail_path).

    Raises ``MediaTooLargeError`` as soon as it is clear the result will not fit
    ``max_bytes`` (so callers can retry with a lower quality), and
    ``MediaTimeoutError`` after ``max_seconds``.
    """
    import yt_dlp

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    max_seconds = max_seconds or _env_int("DOWNLOAD_TIMEOUT", DEFAULT_DOWNLOAD_TIMEOUT)
    bitrate = int(audio_bitrate or audio_bitrate_ladder()[0])
    started = time.monotonic()
    received: dict[str, int] = {}

    def guarded_hook(data: dict) -> None:
        status = data.get("status")
        if status in {"downloading", "finished"}:
            name = str(data.get("filename") or "")
            received[name] = int(data.get("downloaded_bytes") or data.get("total_bytes") or 0)
        if status == "downloading":
            if time.monotonic() - started > max_seconds:
                raise MediaTimeoutError("download exceeded the time limit")
            if mode == "video":
                exact = data.get("total_bytes") or 0
                estimate = data.get("total_bytes_estimate") or 0
                if exact > max_bytes or estimate > max_bytes * 1.5 or sum(received.values()) > max_bytes:
                    raise MediaTooLargeError("video will not fit Telegram's upload limit")
            else:
                duration = (data.get("info_dict") or {}).get("duration")
                if duration and duration * bitrate * 125 > max_bytes:  # kbps -> bytes/second
                    raise MediaTooLargeError("track is too long for this MP3 bitrate")
        if progress_hook is not None:
            try:
                progress_hook(data)
            except Exception:  # a broken UI callback must never abort the download
                pass

    options = ytdlp_base_options(destination)
    options.update({
        "outtmpl": str(destination / "%(id)s.%(ext)s"),
        "format": format_selector(quality, mode, max_bytes),
        "progress_hooks": [guarded_hook],
        "match_filter": _reject_live,
        "overwrites": True,
        "writethumbnail": mode == "audio",
    })
    if mode == "video":
        options["merge_output_format"] = "mp4"
    else:
        options["postprocessors"] = [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(bitrate)},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ]

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(query, download=True)
    except yt_dlp.utils.DownloadError as exc:
        inner = exc.exc_info[1] if getattr(exc, "exc_info", None) else None
        if isinstance(inner, MediaDownloadError):
            raise inner from exc
        raise

    if info and "entries" in info:
        info = next((entry for entry in info["entries"] if entry), None)
    if not info:
        raise MediaNotFoundError("No media result found")
    if info.get("is_live") or info.get("live_status") in LIVE_STATES:
        raise LiveStreamError("live streams are not supported")

    media_path = _locate_output(destination, info, mode)
    if media_path.stat().st_size > max_bytes:
        raise MediaTooLargeError("file exceeds the upload limit")
    thumbnails = [p for p in destination.iterdir() if p.is_file() and p.suffix.lower() in THUMBNAIL_SUFFIXES]
    thumbnail_path = max(thumbnails, key=lambda path: path.stat().st_mtime) if thumbnails else None
    return media_path, info, thumbnail_path
