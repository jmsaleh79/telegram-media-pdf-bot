"""yt-dlp helpers. All network/media work is called from a worker thread."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

ALLOWED_DOMAINS = (
    "youtube.com", "youtube-nocookie.com", "youtu.be", "tiktok.com",
    "instagram.com", "facebook.com", "fb.watch", "twitter.com", "x.com",
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


def inspect_media(query: str) -> dict:
    """Read metadata without downloading. Query may be a URL or yt-dlp search."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
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
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": str(destination / "%(id)s.%(ext)s"),
        "format": format_selector(quality, mode),
        "progress_hooks": [progress_hook] if progress_hook else [],
        "writethumbnail": True,
        "overwrites": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 30,
    }
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
