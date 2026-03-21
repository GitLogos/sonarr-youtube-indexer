#!/usr/bin/env python3
"""
YouTube Indexer for Prowlarr/Sonarr (Torznab)
=============================================
A Torznab-compatible indexer that searches YouTube using yt-dlp and returns
results in Torznab/RSS format for Sonarr/Prowlarr.

Enhancements in this fork:
- Prowlarr Test compatibility: handles t=search without q
- Always emits pubDate per RSS item (Prowlarr requirement)
- Optional parallel language hint enrichment (best-effort) while keeping extract_flat=True
  for the initial ytsearch for speed.
- Always emits torznab:attr "languages" (fallback to "und" when unknown)

Author: Ioannis Kokkinis (original)
License: MIT
"""

import os
import hashlib
import urllib.parse
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from xml.etree.ElementTree import Element, SubElement, tostring
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Tuple, Optional

# Try to import yt-dlp for YouTube search
try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False
    print("WARNING: yt-dlp not installed. Install with: pip install yt-dlp")

# -----------------------------------------------------------------------------
# Configuration (can be overridden by environment variables)
# -----------------------------------------------------------------------------
CONFIG = {
    "host": os.getenv("HOST", "0.0.0.0"),
    "port": int(os.getenv("PORT", "9117")),
    "api_key": os.getenv("API_KEY", "youtubeindexer"),
    "indexer_name": os.getenv("INDEXER_NAME", "YouTube"),
    "log_level": os.getenv("LOG_LEVEL", "INFO"),

    # Prowlarr "Test" may call: t=search&extended=1&apikey=... with NO q
    "fallback_query": os.getenv("FALLBACK_QUERY", "kurzgesagt"),
    "fallback_max_results": int(os.getenv("FALLBACK_MAX_RESULTS", "5")),
    "fallback_cache_ttl_sec": int(os.getenv("FALLBACK_CACHE_TTL_SEC", "3600")),

    # Language enrichment (best-effort)
    # Keep extract_flat=True for ytsearch (fast), and enrich top K in parallel
    "enable_language_hints": os.getenv("ENABLE_LANGUAGE_HINTS", "false").lower() in ("1", "true", "yes", "on"),
    "lang_hint_max_enrich": int(os.getenv("LANG_HINT_MAX_ENRICH", "15")),  # <-- DEFAULT CHANGED TO 15
    "lang_hint_workers": int(os.getenv("LANG_HINT_WORKERS", "6")),
    "lang_hint_cache_ttl_sec": int(os.getenv("LANG_HINT_CACHE_TTL_SEC", "86400")),
    "yt_socket_timeout": int(os.getenv("YT_SOCKET_TIMEOUT", "15")),
}

logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"].upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Simple in-memory cache for fallback query results
_FALLBACK_CACHE = {"ts": 0.0, "videos": []}

# Simple in-memory cache for language hints by video_id
# video_id -> (timestamp, [langs...])
_LANG_CACHE: Dict[str, Tuple[float, List[str]]] = {}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

def _rfc822(dt: datetime) -> str:
    """Format as RFC-822 / RFC-1123 string for RSS pubDate."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")

def generate_guid(video_id: str) -> str:
    return hashlib.md5(video_id.encode("utf-8")).hexdigest()

def _ensure_watch_url(video_id_or_url: str) -> str:
    """Ensure we have a proper watch URL for a YouTube video."""
    if not video_id_or_url:
        return ""
    if video_id_or_url.startswith("http://") or video_id_or_url.startswith("https://"):
        return video_id_or_url
    return f"https://www.youtube.com/watch?v={video_id_or_url}"


# -----------------------------------------------------------------------------
# yt-dlp calls
# -----------------------------------------------------------------------------
def search_youtube_flat(query: str, max_results: int = 20) -> List[dict]:
    """Fast YouTube search using ytsearch in extract_flat mode."""
    if not HAS_YTDLP:
        logger.error("yt-dlp not available")
        return []

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "socket_timeout": CONFIG["yt_socket_timeout"],
    }

    search_url = f"ytsearch{max_results}:{query}"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(search_url, download=False)

        videos = []
        if result and "entries" in result:
            for entry in result["entries"]:
                if not entry:
                    continue

                vid = entry.get("id", "") or entry.get("url", "")
                url = entry.get("url") or _ensure_watch_url(vid)

                videos.append({
                    "id": entry.get("id", ""),
                    "title": entry.get("title", "Unknown"),
                    "url": url,
                    "channel": entry.get("channel", entry.get("uploader", "Unknown")),
                    "duration": entry.get("duration", 0),
                    "view_count": entry.get("view_count", 0),
                    "upload_date": entry.get("upload_date", ""),  # usually missing in flat mode
                    "description": entry.get("description", ""),
                    # language fields will be enriched optionally
                    "language": "",
                    "language_hints": [],
                })

        return videos
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
        return []


def _extract_language_hints_for_url(url: str) -> Tuple[str, List[str]]:
    """
    Best-effort language extraction for a single YouTube URL using yt-dlp.
    Returns (primary_language, all_languages).

    Primary language is best-effort:
      - info.language if present, else first from hints, else "".
    Hints are derived from:
      - subtitles keys
      - automatic_captions keys
    """
    if not HAS_YTDLP or not url:
        return ("", [])

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,  # full info for a single video
        "socket_timeout": CONFIG["yt_socket_timeout"],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False) or {}

        langs = set()

        direct = info.get("language") or ""
        if direct:
            langs.add(str(direct))

        for k in ("subtitles", "automatic_captions"):
            m = info.get(k) or {}
            if isinstance(m, dict):
                for code in m.keys():
                    if code:
                        langs.add(str(code))

        all_langs = sorted(langs)
        primary = direct or (all_langs[0] if all_langs else "")
        return (primary, all_langs)

    except Exception as e:
        logger.debug(f"Language extraction failed for {url}: {e}")
        return ("", [])


def enrich_languages_parallel(videos: List[dict]) -> List[dict]:
    """
    Enrich top K videos with language hints in parallel.
    Uses an in-memory cache to avoid repeated yt-dlp calls.
    """
    if not CONFIG["enable_language_hints"]:
        return videos

    k = max(0, min(CONFIG["lang_hint_max_enrich"], len(videos)))
    if k == 0:
        return videos

    now = time.time()

    def cached_lookup(video_id: str) -> Optional[Tuple[str, List[str]]]:
        if not video_id:
            return None
        cached = _LANG_CACHE.get(video_id)
        if not cached:
            return None
        ts, langs = cached
        if (now - ts) > CONFIG["lang_hint_cache_ttl_sec"]:
            return None
        primary = langs[0] if langs else ""
        return (primary, langs)

    to_enrich = []
    for i in range(k):
        vid = videos[i]
        video_id = vid.get("id", "")
        cached = cached_lookup(video_id)
        if cached:
            primary, all_langs = cached
            vid["language"] = primary
            vid["language_hints"] = all_langs
        else:
            to_enrich.append((i, vid))

    if not to_enrich:
        return videos

    workers = max(1, CONFIG["lang_hint_workers"])
    logger.info(f"Enriching language hints for {len(to_enrich)} videos (parallel, workers={workers})")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_map = {}
        for idx, vid in to_enrich:
            url = _ensure_watch_url(vid.get("url") or vid.get("id", ""))
            future = ex.submit(_extract_language_hints_for_url, url)
            future_map[future] = (idx, vid)

        for future in as_completed(future_map):
            idx, vid = future_map[future]
            try:
                primary, all_langs = future.result()
            except Exception:
                primary, all_langs = ("", [])

            vid["language"] = primary
            vid["language_hints"] = all_langs

            video_id = vid.get("id", "")
            if video_id:
                _LANG_CACHE[video_id] = (time.time(), all_langs)

    return videos


def get_fallback_videos() -> List[dict]:
    """Return cached fallback results (refreshing when TTL expires)."""
    now = time.time()
    if _FALLBACK_CACHE["videos"] and (now - _FALLBACK_CACHE["ts"] < CONFIG["fallback_cache_ttl_sec"]):
        return _FALLBACK_CACHE["videos"]

    q = CONFIG["fallback_query"]
    logger.info(f"Fallback search (for empty query): {q}")
    vids = search_youtube_flat(q, max_results=CONFIG["fallback_max_results"])
    vids = enrich_languages_parallel(vids)
    _FALLBACK_CACHE["ts"] = now
    _FALLBACK_CACHE["videos"] = vids
    return vids


# -----------------------------------------------------------------------------
# Torznab XML
# -----------------------------------------------------------------------------
def format_torznab_xml(videos: List[dict], query: str = "") -> str:
    """Format results as Torznab XML."""
    rss = Element("rss", {
        "version": "2.0",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
        "xmlns:torznab": "http://torznab.com/schemas/2015/feed",
    })

    channel = SubElement(rss, "channel")

    title = SubElement(channel, "title")
    title.text = CONFIG["indexer_name"]

    description = SubElement(channel, "description")
    description.text = "YouTube Video Indexer for Sonarr"

    SubElement(channel, "{http://www.w3.org/2005/Atom}link", {
        "href": f"http://localhost:{CONFIG['port']}/api",
        "rel": "self",
        "type": "application/rss+xml",
    })

    for video in videos:
        item = SubElement(channel, "item")

        item_title = SubElement(item, "title")
        item_title.text = video.get("title", "Unknown")

        guid = SubElement(item, "guid")
        guid.text = generate_guid(video.get("id", "") or video.get("url", ""))

        link = SubElement(item, "link")
        link.text = video.get("url", "")

        comments = SubElement(item, "comments")
        comments.text = f"Channel: {video.get('channel', 'Unknown')}"

        # pubDate is REQUIRED by Prowlarr for each item
        upload_date = (video.get("upload_date") or "").strip()
        if upload_date:
            try:
                pub_dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            except Exception:
                pub_dt = _now_utc()
        else:
            pub_dt = _now_utc()

        pub_date_elem = SubElement(item, "pubDate")
        pub_date_elem.text = _rfc822(pub_dt)

        # Size estimate: ~5MB per minute
        duration = video.get("duration") or 600
        duration_mins = float(duration) / 60.0
        estimated_size = int(duration_mins * 5 * 1024 * 1024)

        size = SubElement(item, "size")
        size.text = str(estimated_size)

        category = SubElement(item, "category")
        category.text = "5000"

        SubElement(item, "enclosure", {
            "url": video.get("url", ""),
            "length": str(estimated_size),
            "type": "application/x-bittorrent",
        })

        # Standard torznab-ish attrs
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "category", "value": "5000"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "seeders", "value": "100"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "peers", "value": "100"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "downloadvolumefactor", "value": "0"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "uploadvolumefactor", "value": "1"})

        # ---------------------------------------------------------------------
        # Language attrs (best-effort)
        # - ALWAYS emit languages (fallback "und" if unknown)
        # - Emit language if we have a primary (direct or inferred)
        # ---------------------------------------------------------------------
        primary_lang = (video.get("language") or "").strip()
        hints = video.get("language_hints") or []
        hints = [h for h in hints if h]  # sanitize

        # Always emit languages
        languages_value = ",".join(hints) if hints else "und"
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {
            "name": "languages",
            "value": languages_value,
        })

        # If we don't have a primary but we have hints, pick first as primary
        if not primary_lang and hints:
            primary_lang = hints[0]

        if primary_lang:
            SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {
                "name": "language",
                "value": primary_lang,
            })

    return tostring(rss, encoding="unicode")


def get_capabilities_xml() -> str:
    """Return Torznab capabilities XML."""
    caps = Element("caps")

    SubElement(caps, "server", {"version": "1.0", "title": CONFIG["indexer_name"]})
    SubElement(caps, "limits", {"max": "100", "default": "20"})

    searching = SubElement(caps, "searching")
    SubElement(searching, "search", {"available": "yes", "supportedParams": "q"})
    SubElement(searching, "tv-search", {"available": "yes", "supportedParams": "q,season,ep"})
    SubElement(searching, "movie-search", {"available": "no"})
    SubElement(searching, "music-search", {"available": "no"})
    SubElement(searching, "audio-search", {"available": "no"})
    SubElement(searching, "book-search", {"available": "no"})

    categories = SubElement(caps, "categories")
    cat = SubElement(categories, "category", {"id": "5000", "name": "TV"})
    SubElement(cat, "subcat", {"id": "5030", "name": "TV/SD"})
    SubElement(cat, "subcat", {"id": "5040", "name": "TV/HD"})
    SubElement(cat, "subcat", {"id": "5045", "name": "TV/UHD"})
    SubElement(cat, "subcat", {"id": "5050", "name": "TV/Other"})

    return tostring(caps, encoding="unicode")


# -----------------------------------------------------------------------------
# HTTP Handler
# -----------------------------------------------------------------------------
class TorznabHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.debug("HTTP: " + (fmt % args))

    def _send_xml(self, xml_content: str):
        body = xml_content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str):
        error_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<error code="{code}" description="{message}"/>'
        )
        self._send_xml(error_xml)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # Serve only /api (and / as convenience)
        if parsed.path not in ("/api", "/"):
            self._send_error(201, f"Unknown path: {parsed.path}")
            return

        params = urllib.parse.parse_qs(parsed.query)

        # API key validation
        apikey = params.get("apikey", [""])[0]
        if CONFIG["api_key"] and apikey != CONFIG["api_key"]:
            logger.warning(f"Invalid API key: {apikey}")
            self._send_error(100, "Invalid API Key")
            return

        action = params.get("t", [""])[0].lower()
        logger.info(f"Request: action={action}, params={params}")

        # If t is omitted, behave like caps
        if action in ("", "caps"):
            xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + get_capabilities_xml()
            self._send_xml(xml)
            return

        if action in ("search", "tvsearch"):
            # Base query
            q = params.get("q", [""])[0].strip()

            # Prowlarr Test may call t=search without q
            if not q:
                videos = get_fallback_videos()
                xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(videos, CONFIG["fallback_query"])
                self._send_xml(xml)
                return

            logger.info(f"Searching YouTube for: {q}")
            videos = search_youtube_flat(q, max_results=20)
            videos = enrich_languages_parallel(videos)
            logger.info(f"Found {len(videos)} results")

            xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(videos, q)
            self._send_xml(xml)
            return

        if action == "download":
            link = params.get("link", params.get("id", [""]))[0]
            if link:
                self.send_response(302)
                self.send_header("Location", link)
                self.end_headers()
            else:
                self._send_error(200, "Missing download link")
            return

        self._send_error(201, f"Unknown action: {action}")


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         YouTube Indexer for Prowlarr/Sonarr                  ║
║         Torznab-compatible API Server                        ║
╠══════════════════════════════════════════════════════════════╣
║  Add this to Prowlarr as a Generic Torznab indexer:          ║
║  URL: http://localhost:{CONFIG["port"]}                      ║
║  API Path: /api                                              ║
║  API Key: {CONFIG["api_key"]}                                ║
║                                                              ║
║  Language hints enabled: {CONFIG["enable_language_hints"]}    ║
║  LANG_HINT_MAX_ENRICH default: {CONFIG["lang_hint_max_enrich"]}║
╚══════════════════════════════════════════════════════════════╝
""")

    if not HAS_YTDLP:
        print("ERROR: yt-dlp is required. Install with: pip install yt-dlp")
        return

    server = HTTPServer((CONFIG["host"], CONFIG["port"]), TorznabHandler)
    logger.info(f"Starting Torznab server on {CONFIG['host']}:{CONFIG['port']}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
