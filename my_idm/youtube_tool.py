"""YouTube / video-site download integration powered by yt-dlp.

My-IDM never speaks YouTube's protocols directly. All extraction is delegated
to `yt-dlp`, which is integrated as a Python library (preferred) or as an
external binary. Two download modes are supported:

* **Mode A** — resolve a direct CDN stream URL and hand it to ``HTTPEngine``
  for segmented, resumable, throttled, VPN/Tor-aware downloading.
* **Mode B** — let yt-dlp perform the download itself so that ffmpeg can merge
  separate video+audio streams, then relay progress hooks back to the UI.

See ``docs/architecture/youtube-scraper.md`` for the full design.
"""

from __future__ import annotations

import logging
import re
import glob
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from my_idm.config import (
    DEFAULT_YTDLP_FORMAT,
    DEFAULT_YTDLP_PLAYLIST_LIMIT,
    ExternalToolsConfig,
    clamp_ytdlp_playlist_limit,
)
from my_idm.proc import background_kwargs
from my_idm.utils import sanitize_filename, split_extension

log = logging.getLogger(__name__)

YTDLP_INSTALL_HINT = (
    "yt-dlp is not installed. Install it with:\n\n"
    "    pip install -U yt-dlp\n\n"
    "or download the standalone binary from https://github.com/yt-dlp/yt-dlp"
)

SUPPORTED_BROWSERS = ("chrome", "firefox", "edge", "brave", "opera", "vivaldi", "safari", "chromium")

# Matches the video page itself, not every YouTube URL shape.
_YOUTUBE_URL_RE = re.compile(
    r"""(?ix)
    \A
    https?://
    (?: www\.| m\.| music\.)*
    (?:
        youtube\.com
        | youtu\.be
        | youtube-nocookie\.com
    )
    (?:
        /watch\?(?:[^#\s&]*&)*v=[A-Za-z0-9_-]{6,}
      | /watch\?(?:[^#\s&]*&)*list=[A-Za-z0-9_-]+
      | /shorts/[A-Za-z0-9_-]{6,}
      | /embed/[A-Za-z0-9_-]{6,}
      | /live/[A-Za-z0-9_-]{6,}
      | /playlist\?(?:[^#\s&]*&)*list=[A-Za-z0-9_-]+
      | /v/[A-Za-z0-9_-]{6,}
      | /[A-Za-z0-9_-]{6,}
    )
    [^\s<>"]*
    \Z
    """
)

_PLAYLIST_PATH_RE = re.compile(r"(?:^|[?&])list=[A-Za-z0-9_-]+")

# Minimum gap between extraction requests. YouTube rate-limits aggressive clients
# (HTTP 429 / bot checks), so analyses are spaced out and identical URLs are
# served from a short-lived cache instead of re-fetched.
_MIN_REQUEST_INTERVAL_SECONDS = 0.75
_ANALYSIS_CACHE_TTL_SECONDS = 300.0

_request_lock = threading.Lock()
_last_request_at = 0.0
_analysis_cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()


def _throttle() -> None:
    """Block briefly so consecutive extraction requests are not back-to-back."""
    global _last_request_at
    with _request_lock:
        wait = _MIN_REQUEST_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _cache_get(key: str) -> Optional[Any]:
    """Return a recent cached result for *key*, or None."""
    with _request_lock:
        entry = _analysis_cache.get(key)
        if entry is None:
            return None
        stamp, value = entry
        if time.monotonic() - stamp > _ANALYSIS_CACHE_TTL_SECONDS:
            _analysis_cache.pop(key, None)
            return None
        return value


def _cache_put(key: str, value: Any) -> None:
    with _request_lock:
        _analysis_cache[key] = (time.monotonic(), value)
        while len(_analysis_cache) > 8:
            _analysis_cache.popitem(last=False)


def clear_analysis_cache() -> None:
    """Drop cached extraction results (used by tests and settings changes)."""
    with _request_lock:
        _analysis_cache.clear()


class YouTubeToolError(RuntimeError):
    """Raised when yt-dlp is unavailable or extraction fails."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


@dataclass
class YouTubeFormat:
    """A single downloadable stream offered by yt-dlp."""

    format_id: str
    ext: str = ""
    resolution: str = "audio only"
    fps: Optional[float] = None
    vcodec: str = "none"
    acodec: str = "none"
    filesize: Optional[int] = None
    url: str = ""
    format_note: str = ""
    tbr: Optional[float] = None
    protocol: str = ""
    has_video: bool = False
    has_audio: bool = False
    is_video_only: bool = False
    is_audio_only: bool = False
    is_muxed: bool = False
    requires_merge: bool = False
    direct_capable: bool = False
    http_headers: dict = field(default_factory=dict)

    @property
    def quality_label(self) -> str:
        """Human-readable size string for the format table."""
        if not self.filesize:
            return "?"
        size = float(self.filesize)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"

    @property
    def display_resolution(self) -> str:
        """Resolution label with height preferred over raw pixel dimensions."""
        if self.is_audio_only:
            return "audio only"
        match = re.match(r"(\d+)x(\d+)", self.resolution or "")
        if match:
            return f"{match.group(2)}p"
        return self.resolution or "?"

    @property
    def display_fps(self) -> str:
        if not self.fps:
            return "-"
        return f"{self.fps:.0f}" if float(self.fps).is_integer() else f"{self.fps:g}"


@dataclass
class YouTubeMetadata:
    """Extracted metadata for a single video, playlist, or channel batch."""

    id: str = ""
    title: str = "Unknown"
    thumbnail: str = ""
    duration: Optional[float] = None
    uploader: str = ""
    upload_date: str = ""
    description: str = ""
    webpage_url: str = ""
    extractor: str = ""
    ext: str = ""
    formats: list[YouTubeFormat] = field(default_factory=list)
    entries: list["YouTubeMetadata"] = field(default_factory=list)
    is_playlist: bool = False
    is_live: bool = False
    age_restricted: bool = False
    available_formats_count: int = 0

    @property
    def is_batch(self) -> bool:
        """True when this entry represents a playlist/channel with children."""
        return self.is_playlist and bool(self.entries)

    @property
    def duration_label(self) -> str:
        """Duration formatted as ``H:MM:SS`` / ``M:SS``."""
        if not self.duration or self.duration <= 0:
            return ""
        total = int(self.duration)
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"

    @property
    def upload_date_label(self) -> str:
        """Upload date converted from ``YYYYMMDD`` to ``YYYY-MM-DD``."""
        raw = (self.upload_date or "").strip()
        if len(raw) == 8 and raw.isdigit():
            return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
        return raw

    @property
    def direct_formats(self) -> list[YouTubeFormat]:
        """Formats that can be downloaded directly by HTTPEngine (Mode A)."""
        return [f for f in self.formats if f.direct_capable]

    def find_format(self, format_id: str) -> Optional[YouTubeFormat]:
        """Return the format matching *format_id*, or None."""
        for fmt in self.formats:
            if fmt.format_id == format_id:
                return fmt
        return None


@dataclass
class DirectUrlResult:
    """Outcome of resolving a Mode A direct stream URL."""

    direct_url: str
    filename: str
    filesize: Optional[int] = None
    content_type: str = ""
    format_id: str = ""
    webpage_url: str = ""
    expires_at: Optional[float] = None
    http_headers: dict = field(default_factory=dict)


def _import_ytdlp():
    """Import and return the ``yt_dlp`` module, or raise a helpful error."""
    try:
        import yt_dlp  # noqa: PLC0415
    except ImportError as exc:
        raise YouTubeToolError(YTDLP_INSTALL_HINT, kind="not_installed") from exc
    return yt_dlp


def _require_ytdlp() -> None:
    """Raise :class:`YouTubeToolError` when the yt-dlp module cannot be imported."""
    _import_ytdlp()


def check_ytdlp_available(config: Optional[ExternalToolsConfig] = None) -> bool:
    """Return True when yt-dlp is importable or its binary is discoverable."""
    if config is not None and config.get_effective_ytdlp_path():
        return True
    try:
        _require_ytdlp()
        return True
    except YouTubeToolError:
        return False


def get_ytdlp_version(config: Optional[ExternalToolsConfig] = None) -> str:
    """Return the installed yt-dlp version string, or an empty string."""
    try:
        module = _import_ytdlp()
    except YouTubeToolError:
        module = None

    if module is not None:
        version = getattr(module, "version", None)
        if isinstance(version, str) and version:
            return version
        inner = getattr(version, "__version__", None)
        if isinstance(inner, str) and inner:
            return inner

    binary = config.get_effective_ytdlp_path() if config else (shutil.which("yt-dlp") or "")
    if not binary:
        return ""
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            **background_kwargs(detach=False),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("Failed to query yt-dlp version: %s", exc)
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or "").strip()


def check_ffmpeg_available(config: Optional[ExternalToolsConfig] = None) -> bool:
    """Return True when ffmpeg is available on PATH or at the configured path."""
    if config is not None and config.get_effective_ffmpeg_path():
        return True
    return bool(shutil.which("ffmpeg"))


def detect_youtube_url(text: str) -> Optional[str]:
    """Return the first YouTube video/playlist URL found in *text*, else None.

    Matches the canonical watch, shorts, embed, live, and playlist forms plus
    the youtu.be shortener. Non-YouTube URLs always return None.
    """
    if not text or not isinstance(text, str):
        return None
    for token in re.split(r"[\s<>\"']+", text.strip()):
        candidate = token.strip().rstrip(".,;)")
        if not candidate:
            continue
        if _YOUTUBE_URL_RE.match(candidate):
            return candidate
    return None


def is_playlist_url(url: str) -> bool:
    """Return True when *url* points at a playlist or mixes a video with a list."""
    if not url:
        return False
    if re.search(r"youtube\.com/playlist\?", url, re.IGNORECASE):
        return True
    return bool(_PLAYLIST_PATH_RE.search(url))


def _build_ydl_opts(config: Optional[ExternalToolsConfig], mode: str = "extract") -> dict:
    """Assemble yt-dlp options from user configuration."""
    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": False,
    }
    if config is None:
        return options

    ffmpeg = config.get_effective_ffmpeg_path()
    if ffmpeg:
        options["ffmpeg_location"] = ffmpeg

    browser = (config.ytdlp_cookies_browser or "").strip().lower()
    if browser and browser != "none" and browser in SUPPORTED_BROWSERS:
        options["cookiesfrombrowser"] = (browser,)

    if mode == "extract":
        options["extract_flat"] = False
        options["format"] = config.ytdlp_default_format or DEFAULT_YTDLP_FORMAT
    elif mode == "playlist":
        options["extract_flat"] = "in_playlist"
        options["skip_download"] = True

    return options


def _classify_error(exc: Exception) -> YouTubeToolError:
    """Translate a yt-dlp exception into a classified :class:`YouTubeToolError`."""
    message = str(exc) or exc.__class__.__name__
    lowered = message.lower()

    if "confirm your age" in lowered or "age-restricted" in lowered or "age restricted" in lowered:
        return YouTubeToolError(
            f"This video is age-restricted.\n\n{message}\n\n"
            "Set a cookie source in Settings → External Tools → YouTube to access it.",
            kind="age_restricted",
        )
    if (
        "not available in your country" in lowered
        or "not made this video available" in lowered
        or "geo-restricted" in lowered
        or "geo restricted" in lowered
        or "geo blocked" in lowered
    ):
        return YouTubeToolError(
            f"This video is geo-restricted.\n\n{message}\n\n"
            "Enable VPN or Tor routing in Settings → Network & Privacy and try again.",
            kind="geo_restricted",
        )
    if (
        "sign in" in lowered
        or "login required" in lowered
        or "private video" in lowered
        or "members-only" in lowered
        or "this video is available to this channel's members" in lowered
    ):
        return YouTubeToolError(
            f"This video is private or requires authentication.\n\n{message}\n\n"
            "Set a cookie source in Settings → External Tools → YouTube to access it.",
            kind="auth",
        )
    if "unsupported url" in lowered:
        return YouTubeToolError(f"yt-dlp does not support this site.\n\n{message}", kind="unsupported")
    if ("rate" in lowered and "limit" in lowered) or "too many requests" in lowered or "http error 429" in lowered:
        return YouTubeToolError(f"Rate limited by the server. Please try again later.\n\n{message}", kind="rate_limited")
    if isinstance(exc, YouTubeToolError):
        return exc
    return YouTubeToolError(message, kind="error")


def _normalize_codec(value: Any) -> str:
    """Return a lowercased codec name, mapping missing/None values to ``none``."""
    if not value or not isinstance(value, str):
        return "none"
    cleaned = value.strip().lower()
    return cleaned or "none"


def _parse_format(raw: dict) -> Optional[YouTubeFormat]:
    """Convert a yt-dlp format dict into :class:`YouTubeFormat`."""
    if not isinstance(raw, dict):
        return None
    format_id = raw.get("format_id")
    if format_id is None:
        return None

    vcodec = _normalize_codec(raw.get("vcodec"))
    acodec = _normalize_codec(raw.get("acodec"))
    has_video = vcodec != "none"
    has_audio = acodec != "none"
    is_video_only = has_video and not has_audio
    is_audio_only = has_audio and not has_video

    filesize = raw.get("filesize") or raw.get("filesize_approx")
    try:
        filesize = int(filesize) if filesize else None
    except (TypeError, ValueError):
        filesize = None

    fps = raw.get("fps")
    try:
        fps = float(fps) if fps else None
    except (TypeError, ValueError):
        fps = None

    tbr = raw.get("tbr") or raw.get("vbr") or raw.get("abr")
    try:
        tbr = float(tbr) if tbr else None
    except (TypeError, ValueError):
        tbr = None

    url = str(raw.get("url") or "")
    requires_merge = is_video_only or is_audio_only
    raw_headers = raw.get("http_headers")
    http_headers = (
        {str(k): str(v) for k, v in raw_headers.items() if v is not None}
        if isinstance(raw_headers, dict)
        else {}
    )

    return YouTubeFormat(
        format_id=str(format_id),
        ext=str(raw.get("ext") or ""),
        resolution=str(raw.get("resolution") or ("audio only" if is_audio_only else "?")),
        fps=fps,
        vcodec=vcodec,
        acodec=acodec,
        filesize=filesize,
        url=url,
        format_note=str(raw.get("format_note") or ""),
        tbr=tbr,
        protocol=str(raw.get("protocol") or ""),
        has_video=has_video,
        has_audio=has_audio,
        is_video_only=is_video_only,
        is_audio_only=is_audio_only,
        is_muxed=has_video and has_audio,
        requires_merge=requires_merge,
        direct_capable=bool(url) and not is_video_only,
        http_headers=http_headers,
    )


def _parse_formats(raw_formats: Any) -> list[YouTubeFormat]:
    """Parse the ``formats`` list, dropping storyboards and duplicates."""
    parsed: list[YouTubeFormat] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in raw_formats or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("format_note") == "storyboard" or raw.get("ext") == "mhtml":
            continue
        fmt = _parse_format(raw)
        if fmt is None:
            continue
        key = (fmt.format_id, fmt.ext, fmt.resolution)
        if key in seen:
            continue
        seen.add(key)
        parsed.append(fmt)
    return parsed


def _build_metadata(info: dict, source_url: str) -> YouTubeMetadata:
    """Build :class:`YouTubeMetadata` from a yt-dlp info dict."""
    formats = _parse_formats(info.get("formats"))
    return YouTubeMetadata(
        id=str(info.get("id") or ""),
        title=str(info.get("title") or "Unknown"),
        thumbnail=str(info.get("thumbnail") or ""),
        duration=info.get("duration"),
        uploader=str(info.get("uploader") or info.get("channel") or ""),
        upload_date=str(info.get("upload_date") or ""),
        description=str(info.get("description") or ""),
        webpage_url=str(info.get("webpage_url") or source_url),
        extractor=str(info.get("extractor_key") or info.get("extractor") or ""),
        ext=str(info.get("ext") or ""),
        formats=formats,
        is_playlist=bool(info.get("entries") is not None and "entries" in info),
        is_live=bool(info.get("is_live")),
        age_restricted=bool(info.get("age_restrict")),
        available_formats_count=int(info.get("formats_count") or len(formats)),
    )


def _config_fingerprint(config: Optional[ExternalToolsConfig]) -> str:
    """Identify the settings that change what a given URL extracts to.

    Cookie source and ffmpeg location materially affect the result, so cached
    entries must not be reused across different configurations.
    """
    if config is None:
        return "-"
    return "|".join(
        (
            str(config.get_effective_ytdlp_path()),
            str(config.get_effective_ffmpeg_path()),
            str(config.ytdlp_cookies_browser or ""),
            str(config.ytdlp_default_format or ""),
            str(bool(config.ytdlp_enabled)),
        )
    )


def _extract_info(url: str, config: Optional[ExternalToolsConfig], mode: str = "extract") -> dict:
    """Run ``extract_info(download=False)`` and return the raw info dict."""
    module = _import_ytdlp()
    options = _build_ydl_opts(config, mode=mode)
    cache_key = f"{mode}|{_config_fingerprint(config)}|{url}"
    cached = _cache_get(cache_key)
    if cached is not None:
        log.debug("Using cached extraction for %s", url)
        return cached
    _throttle()
    try:
        with module.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False) or {}
    except YouTubeToolError:
        raise
    except Exception as exc:
        raise _classify_error(exc) from exc
    _cache_put(cache_key, info)
    return info


def extract_metadata(url: str, config: Optional[ExternalToolsConfig] = None) -> YouTubeMetadata:
    """Extract metadata and available formats for a single video URL.

    Raises :class:`YouTubeToolError` when yt-dlp is missing or extraction fails.
    """
    if not url or not str(url).strip():
        raise YouTubeToolError("No URL provided.", kind="invalid_url")

    info = _extract_info(str(url).strip(), config, mode="extract")
    if not info:
        raise YouTubeToolError("yt-dlp returned no information for this URL.", kind="error")

    metadata = _build_metadata(info, str(url).strip())
    if not metadata.formats:
        metadata.formats = _parse_formats(info.get("requested_formats") or info.get("requested_downloads"))
    return metadata


def _entry_url(entry: dict, parent_url: str) -> str:
    """Best-effort canonical watch URL for a playlist entry.

    Flat playlist entries carry only a bare video id in ``url``, which is not a
    usable URL on its own.
    """
    if not isinstance(entry, dict):
        return parent_url
    webpage = entry.get("webpage_url")
    if isinstance(webpage, str) and webpage.startswith("http"):
        return webpage
    url = entry.get("url")
    if isinstance(url, str) and url.startswith("http"):
        return url
    if isinstance(url, str) and url and "youtube.com" not in parent_url:
        return url
    vid = entry.get("id")
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return parent_url


def _pick_thumbnail(entry: dict) -> str:
    """Return the largest available thumbnail URL for an entry."""
    if not isinstance(entry, dict):
        return ""
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        best = ""
        best_area = -1
        for thumb in thumbs:
            if not isinstance(thumb, dict):
                continue
            url = thumb.get("url")
            if not url:
                continue
            area = (thumb.get("width") or 0) * (thumb.get("height") or 0)
            if area > best_area:
                best_area = area
                best = str(url)
        if best:
            return best
    return str(entry.get("thumbnail") or "")


def _normalize_flat_entry(entry: dict, parent_url: str) -> dict:
    """Turn a flat playlist entry into an info-dict-shaped mapping.

    Flat extraction returns title/id/duration but no formats. Populating these
    fields lets the dialog list a large playlist from a single network request
    instead of re-extracting every video.
    """
    return {
        "id": entry.get("id") or "",
        "title": entry.get("title") or "Unknown",
        "duration": entry.get("duration"),
        "thumbnail": _pick_thumbnail(entry),
        "uploader": entry.get("uploader") or entry.get("channel") or "",
        "webpage_url": _entry_url(entry, parent_url),
        "extractor_key": "Youtube",
        "formats": [],
    }


@dataclass
class PlaylistResult:
    """Outcome of a playlist/channel listing.

    ``total`` is the number of entries the site reported before the configured
    limit was applied, so the UI can tell the user that the list was truncated.
    """

    videos: list[YouTubeMetadata] = field(default_factory=list)
    total: int = 0
    limit: int = 0
    truncated: bool = False
    url: str = ""

    def __len__(self) -> int:
        return len(self.videos)

    def __iter__(self):
        return iter(self.videos)

    def __getitem__(self, index):
        return self.videos[index]


def extract_playlist(
    url: str,
    config: Optional[ExternalToolsConfig] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    limit: Optional[int] = None,
) -> PlaylistResult:
    """Extract entries of a playlist or channel, bounded by a configurable limit.

    A single flat request is used for the whole list; only the first video is
    fully resolved so the dialog can populate its format table. At most two
    network requests are made regardless of how large the playlist is.

    ``config.ytdlp_playlist_limit`` (default 10, max 500) caps how many entries
    are returned, which keeps large playlists from hammering the site. Set
    ``limit`` to override it for one call.
    """
    if not url or not str(url).strip():
        raise YouTubeToolError("No URL provided.", kind="invalid_url")

    if limit is None:
        limit = config.ytdlp_playlist_limit if config is not None else DEFAULT_YTDLP_PLAYLIST_LIMIT
    limit = clamp_ytdlp_playlist_limit(limit)

    info = _extract_info(str(url).strip(), config, mode="playlist")
    if not info:
        raise YouTubeToolError("yt-dlp returned no information for this playlist.", kind="error")

    raw_entries = [e for e in (info.get("entries") or []) if isinstance(e, dict) and e]
    videos_raw = [e for e in raw_entries if "entries" not in e]
    if not videos_raw:
        videos_raw = raw_entries

    total = len(videos_raw)
    selected = videos_raw[:limit]

    videos: list[YouTubeMetadata] = []
    for index, entry in enumerate(selected):
        if cancel_check is not None and cancel_check():
            raise YouTubeToolError("Playlist loading cancelled.", kind="cancelled")

        if index == 0 and not entry.get("formats"):
            resolved = _entry_url(entry, str(url).strip())
            try:
                full = _extract_info(resolved, config, mode="extract")
            except YouTubeToolError:
                full = {}
            if full:
                entry = {**entry, **full}

        normalized = _normalize_flat_entry(entry, str(url).strip())
        if index == 0 and entry.get("formats"):
            normalized["formats"] = entry.get("formats") or []
        videos.append(_build_metadata(normalized, normalized["webpage_url"]))

    return PlaylistResult(
        videos=videos,
        total=total,
        limit=limit,
        truncated=total > len(videos),
        url=str(url).strip(),
    )


def build_stem(title: str, max_length: int = 150) -> str:
    """Return a filesystem-safe filename stem (no extension) for a video title.

    Titles routinely contain path separators and other characters that are
    illegal on Windows. yt-dlp rewrites those itself (e.g. ``/`` becomes ``/``),
    which would make the produced file disagree with the name recorded in the
    database, so My-IDM normalises the stem up front and hands it to yt-dlp as
    an explicit output template.
    """
    cleaned = sanitize_filename(title or "video", max_length=max_length)
    stem, _ext = split_extension(cleaned)
    return stem or "video"


def build_filename(title: str, ext: str, max_length: int = 200) -> str:
    """Return a safe ``<title>.<ext>`` filename for a downloaded stream."""
    clean_ext = re.sub(r"[^A-Za-z0-9]", "", str(ext or "")).lower()
    stem = build_stem(title, max_length=max_length - (len(clean_ext) + 1))
    if not clean_ext:
        return stem
    if stem.lower().endswith(f".{clean_ext}"):
        return stem
    return f"{stem}.{clean_ext}"


def resolve_direct_url(
    url: str,
    format_id: str,
    config: Optional[ExternalToolsConfig] = None,
    title: str = "",
) -> DirectUrlResult:
    """Resolve the direct CDN stream URL for *format_id* (Mode A).

    Re-extracts metadata to obtain a freshly signed URL, which matters because
    YouTube CDN links expire after a few hours.
    """
    if not url or not str(url).strip():
        raise YouTubeToolError("No URL provided.", kind="invalid_url")
    if not format_id or not str(format_id).strip():
        raise YouTubeToolError("No format selected.", kind="invalid_format")

    metadata = extract_metadata(str(url).strip(), config)
    fmt = metadata.find_format(str(format_id).strip())
    if fmt is None:
        raise YouTubeToolError(f"Format '{format_id}' is no longer available for this video.", kind="invalid_format")
    if not fmt.url:
        raise YouTubeToolError(
            f"Format '{format_id}' has no direct URL; it requires yt-dlp to download and merge it.",
            kind="merge_required",
        )
    if fmt.is_video_only:
        raise YouTubeToolError(
            f"Format '{format_id}' is video-only and must be merged with an audio track.",
            kind="merge_required",
        )

    filename = build_filename(title or metadata.title, fmt.ext)
    expires_at = None
    for key in ("url_expires", "fragment_base_url_expires"):
        raw_value = None
        if key == "url_expires":
            match = re.search(r"[?&]expire=(\d+)", fmt.url)
            raw_value = match.group(1) if match else None
        if raw_value:
            try:
                expires_at = float(raw_value)
            except (TypeError, ValueError):
                expires_at = None
            break

    return DirectUrlResult(
        direct_url=fmt.url,
        filename=filename,
        filesize=fmt.filesize,
        content_type="video/mp4" if fmt.ext == "mp4" else f"video/{fmt.ext}" if fmt.has_video else f"audio/{fmt.ext}",
        format_id=fmt.format_id,
        webpage_url=metadata.webpage_url,
        expires_at=expires_at,
        http_headers=dict(fmt.http_headers or {}),
    )


def get_download_options(
    config: ExternalToolsConfig,
    format_selector: str,
    save_dir: str,
    outtmpl: Optional[str] = None,
    merge_output_format: str = "",
) -> dict:
    """Build the yt-dlp option dict used for a Mode B native download.

    When *outtmpl* is supplied it overrides yt-dlp's default ``%(title)s`` template
    so the produced file name matches the name My-IDM recorded in its database.
    """
    options = _build_ydl_opts(config, mode="download")
    options.pop("format", None)
    options["format"] = format_selector or config.ytdlp_default_format or DEFAULT_YTDLP_FORMAT
    options["outtmpl"] = outtmpl or str(Path(save_dir) / "%(title)s.%(ext)s")
    options["retries"] = 10
    options["fragment_retries"] = 10
    options["noprogress"] = True
    if merge_output_format:
        options["merge_output_format"] = merge_output_format

    postprocessors: list[dict] = []
    if config.ytdlp_embed_subtitles:
        options["writesubtitles"] = True
        options["writeautomaticsub"] = True
        options["subtitleslangs"] = config.get_effective_ytdlp_subtitle_langs()
        postprocessors.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
    if config.ytdlp_embed_thumbnail:
        postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    if postprocessors:
        options["postprocessors"] = postprocessors

    return options


def resolve_produced_file(save_dir: str, stem: str) -> str:
    """Find the file yt-dlp actually wrote for *stem*, ignoring its temp files.

    yt-dlp merges streams into intermediate ``.f137.mp4`` style parts and may
    leave ``.part`` / ``.ytdl`` files behind, so the exact stem match is tried
    first and only then a filtered scan.
    """
    directory = Path(save_dir)
    if not directory.is_dir() or not stem:
        return ""

    for ext in ("mp4", "mkv", "webm", "m4a", "opus", "mp3", "m4v", "mov"):
        exact = directory / f"{stem}.{ext}"
        if exact.is_file():
            return str(exact)

    for candidate in sorted(directory.glob(f"{glob.escape(stem)}.*")):
        if not candidate.is_file():
            continue
        name = candidate.name
        if name.endswith((".part", ".ytdl", ".temp", ".tmp")):
            continue
        if re.match(r"^\.?f\d+", name) or re.search(r"\.f\d+\.", name):
            continue
        if candidate.suffix.lower() in (".part", ".ytdl", ".temp"):
            continue
        return str(candidate)
    return ""


def start_native_download(
    url: str,
    save_dir: str,
    format_selector: str,
    config: ExternalToolsConfig,
    progress_cb: Optional[Callable[[dict], None]] = None,
    postprocessor_cb: Optional[Callable[[dict], None]] = None,
    done_cb: Optional[Callable[[str], None]] = None,
    error_cb: Optional[Callable[[str], None]] = None,
    cancel_event: Optional[Any] = None,
    outtmpl: Optional[str] = None,
    merge_output_format: str = "",
    expected_total: int = 0,
) -> Any:
    """Start a Mode B yt-dlp download in a daemon thread.

    Returns a tuple ``(thread, cancel_holder)``. Setting the holder's ``cancel``
    attribute aborts the in-flight download. ``done_cb`` receives the absolute
    path of the file that was actually produced.

    ``expected_total`` is the known byte size of the selected formats. Supplying
    it keeps reported progress monotonic, because the running total discovered
    from yt-dlp grows as each stream is reached.
    """
    import threading  # noqa: PLC0415

    module = _import_ytdlp()
    cancel_holder: dict[str, Any] = {"cancel": False}

    # yt-dlp reports progress per stream. A merged video+audio download therefore
    # reports the video stream 0→100% and then restarts at 0% for the audio stream,
    # which makes the UI progress bar jump backwards. Aggregate across streams so
    # the reported figures are monotonic and describe the download as a whole.
    streams: dict[str, tuple[int, int]] = {}

    def _stream_key(status: dict) -> str:
        info = status.get("info_dict")
        if isinstance(info, dict):
            for field in ("format_id", "format", "filename"):
                value = info.get(field)
                if value:
                    return str(value)
        return str(status.get("filename") or "__default__")

    def _progress_hook(status: dict) -> None:
        if not progress_cb:
            return
        try:
            payload = dict(status)
            key = _stream_key(status)
            done = int(status.get("downloaded_bytes") or 0)
            total = int(
                status.get("total_bytes")
                or status.get("total_bytes_estimate")
                or 0
            )
            prev_done, prev_total = streams.get(key, (0, 0))
            # max() keeps each stream monotonic even if yt-dlp re-reports from 0.
            streams[key] = (max(prev_done, done), max(prev_total, total))
            payload["downloaded_bytes"] = sum(d for d, _t in streams.values())
            agg_total = sum(t for _d, t in streams.values())
            if expected_total > 0:
                # Prefer the size we already know, so the total does not jump
                # when a later stream is reached and drag the percentage back.
                payload["total_bytes"] = int(expected_total)
                payload.pop("total_bytes_estimate", None)
            elif agg_total:
                payload["total_bytes"] = agg_total
                payload.pop("total_bytes_estimate", None)
            progress_cb(payload)
        except Exception as exc:
            log.debug("yt-dlp progress hook error: %s", exc)

    def _postprocessor_hook(status: dict) -> None:
        if postprocessor_cb:
            try:
                postprocessor_cb(dict(status))
            except Exception as exc:
                log.debug("yt-dlp postprocessor hook error: %s", exc)

    def _worker() -> None:
        _download_error = None
        try:
            from yt_dlp.utils import DownloadError as _download_error  # noqa: PLC0415
        except Exception:  # pragma: no cover - yt-dlp always provides this
            log.debug("yt_dlp.utils.DownloadError unavailable; using YouTubeToolError for cancel")

        options = get_download_options(
            config, format_selector, save_dir,
            outtmpl=outtmpl, merge_output_format=merge_output_format,
        )
        options["progress_hooks"] = [_progress_hook]
        options["postprocessor_hooks"] = [_postprocessor_hook]

        stem = Path(options["outtmpl"]).name.split("%(")[0].rstrip(". ")
        produced = ""

        try:
            with module.YoutubeDL(options) as ydl:
                def _abort(_status: dict) -> None:
                    if not (cancel_holder.get("cancel") or (cancel_event is not None and cancel_event.is_set())):
                        return
                    # yt-dlp only unwinds cleanly when a progress hook raises
                    # DownloadError; anything else surfaces as
                    # "bad parameter or other API misuse".
                    if _download_error is not None:
                        raise _download_error("Download cancelled by user.")
                    raise YouTubeToolError("Download cancelled by user.", kind="cancelled")

                ydl.add_progress_hook(_abort)
                ydl.download([url])

            produced = resolve_produced_file(save_dir, stem)
            if done_cb:
                done_cb(produced)
        except YouTubeToolError as exc:
            if produced and done_cb:
                done_cb(produced)
            elif error_cb:
                error_cb(str(exc))
        except Exception as exc:
            if produced:
                if done_cb:
                    done_cb(produced)
            elif error_cb:
                error_cb(str(_classify_error(exc)))

    thread = threading.Thread(target=_worker, name="ytdlp-download", daemon=True)
    thread.start()
    return thread, cancel_holder


def update_ytdlp(config: Optional[ExternalToolsConfig] = None) -> tuple[bool, str]:
    """Update yt-dlp via pip or by re-running a downloaded binary.

    Returns ``(success, message)``.
    """
    import sys  # noqa: PLC0415

    binary = config.get_effective_ytdlp_path() if config else ""
    module_available = True
    try:
        _require_ytdlp()
    except YouTubeToolError:
        module_available = False

    if not module_available and binary:
        try:
            completed = subprocess.run(
                [binary, "-U"],
                capture_output=True,
                text=True,
                timeout=300,
                **background_kwargs(detach=False),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"yt-dlp update failed: {exc}"
        if completed.returncode == 0:
            return True, (completed.stdout or "").strip() or "yt-dlp updated."
        return False, (completed.stderr or completed.stdout or "").strip() or "yt-dlp update failed."

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
            capture_output=True,
            text=True,
            timeout=600,
            **background_kwargs(detach=False),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"yt-dlp update failed: {exc}"
    if completed.returncode == 0:
        return True, (completed.stdout or "").strip() or "yt-dlp updated."
    return False, (completed.stderr or completed.stdout or "").strip() or "yt-dlp update failed."
