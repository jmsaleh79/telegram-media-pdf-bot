"""yt-dlp helpers. All network/media work is called from a worker thread."""

from __future__ import annotations

import difflib
import os
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_DOMAINS = (
    "youtube.com", "youtube-nocookie.com", "youtu.be", "tiktok.com",
    "instagram.com", "facebook.com", "fb.watch", "twitter.com", "x.com", "soundcloud.com",
)
QUALITY_HEIGHTS = {"720p": 720, "480p": 480, "360p": 360}


def validate_media_url(url: str) -> bool:
    """Accept only HTTPS URLs hosted on supported video platforms."""
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        host = parsed.hostname.lower().rstrip(".")
        return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS)
    except (ValueError, TypeError):
        return False


def format_selector(quality: str, mode: str) -> str:
    """Create a yt-dlp format selector for the requested output."""
    if mode == "audio":
        return "bestaudio/best"
    if mode != "video":
        raise ValueError("mode must be 'video' or 'audio'")
    height = QUALITY_HEIGHTS.get(quality)
    if height is None:
        if quality != "best":
            raise ValueError("unsupported quality")
        return "bestvideo*+bestaudio/best"
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"


def ytdlp_base_options() -> dict:
    """Shared retry/JS-runtime options and optional private cookies.txt."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "socket_timeout": 30,
        "js_runtimes": {"deno": {}},
    }
    cookie_path = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
    if cookie_path:
        path = Path(cookie_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError("YOUTUBE_COOKIES_FILE points to a missing file")
        with path.open("r", encoding="utf-8", errors="replace") as cookie_file:
            header = cookie_file.readline().strip()
        if header not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
            raise ValueError("Cookie file must use Netscape/Mozilla cookies.txt format")
        options["cookiefile"] = str(path)
    return options


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


def classify_media_error(error: BaseException) -> str:
    """Map yt-dlp/provider errors to stable, user-safe categories."""
    message = str(error).casefold()
    if "youtube_cookies_file" in message or "cookie file must use" in message:
        return "configuration_error"
    if "sign in to confirm" in message or "not a bot" in message or "confirm you're not" in message:
        return "youtube_challenge"
    if "429" in message or "too many requests" in message or "rate limit" in message:
        return "rate_limited"
    if "403" in message or "forbidden" in message:
        return "access_denied"
    if any(term in message for term in ("private video", "login required", "age-restricted", "members-only")):
        return "restricted"
    if any(term in message for term in ("no video results", "no suitable result", "no media result")):
        return "not_found"
    if "unsupported url" in message or "no suitable extractor" in message:
        return "unsupported"
    if any(term in message for term in ("timed out", "timeout", "connection reset", "temporary failure", "http error 5")):
        return "network"
    return "download_error"


def search_songs(query: str, limit: int = 5) -> list[dict]:
    """Search YouTube and SoundCloud, rank candidates, and let users choose."""
    import yt_dlp

    query = query.strip()
    if not query:
        return []
    query = query[:250]
    limit = max(1, min(int(limit), 10))
    options = ytdlp_base_options()
    options["skip_download"] = True
    found: list[dict] = []
    failures: list[Exception] = []
    with yt_dlp.YoutubeDL(options) as ydl:
        for prefix, source in (("ytsearch", "YouTube"), ("scsearch", "SoundCloud")):
            try:
                info = ydl.extract_info(f"{prefix}{limit}:{query}", download=False)
            except Exception as exc:
                failures.append(exc)
                continue
            for entry in (info or {}).get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                url = entry.get("webpage_url")
                if not url and str(entry.get("url") or "").startswith("https://"):
                    url = entry["url"]
                if not url or not validate_media_url(str(url)):
                    continue
                found.append({
                    "title": str(entry.get("title") or "").strip(),
                    "webpage_url": str(url),
                    "uploader": str(entry.get("uploader") or entry.get("artist") or ""),
                    "duration": entry.get("duration"),
                    "thumbnail": entry.get("thumbnail"),
                    "source": source,
                })
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
        raise ValueError("No media result found")
    return info


def download_media(
    query: str,
    output_dir: str | Path,
    mode: str = "video",
    quality: str = "best",
    progress_hook=None,
) -> tuple[Path, dict, Path | None]:
    """Download media, returning (media_path, metadata, thumbnail_path)."""
    import yt_dlp

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    options = ytdlp_base_options()
    options.update({
        "outtmpl": str(destination / "%(id)s.%(ext)s"),
        "format": format_selector(quality, mode),
        "progress_hooks": [progress_hook] if progress_hook else [],
        "writethumbnail": True,
        "overwrites": True,
    })
    if mode == "video":
        options["merge_output_format"] = "mp4"
    else:
        options["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata"},
        ]
        options["writethumbnail"] = True
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(query, download=True)

    if info and "entries" in info:
        info = next((entry for entry in info["entries"] if entry), None)
    if not info:
        raise ValueError("No media result found")

    files = [p for p in destination.iterdir() if p.is_file()]
    if mode == "audio":
        media = [p for p in files if p.suffix.lower() == ".mp3"]
    else:
        media = [p for p in files if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".part", ".json"}]
    if not media:
        raise FileNotFoundError("yt-dlp completed without producing an output file")
    media_path = max(media, key=lambda path: path.stat().st_mtime)
    thumbnails = [p for p in files if p != media_path and p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
    thumbnail_path = max(thumbnails, key=lambda path: path.stat().st_mtime) if thumbnails else None
    return media_path, info, thumbnail_path
