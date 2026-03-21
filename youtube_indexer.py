#!/usr/bin/env python3
"""
YouTube Indexer for Prowlarr/Sonarr
====================================
A Torznab-compatible indexer that searches YouTube and returns results
that Sonarr can use to trigger downloads via the YouTube download client.

This bridges the gap between Sonarr's episode metadata (from TheTVDB) and
actual YouTube video URLs.

Author: Ioannis Kokkinis
License: MIT
"""

import os
import hashlib
import urllib.parse
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from xml.etree.ElementTree import Element, SubElement, tostring
import logging

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

    # Prowlarr "Test" may call: t=search&extended=1&apikey=... with NO q.
    # If empty queries return 0 results, Prowlarr refuses to save the indexer.
    # Use a fallback query so the test returns at least one item.
    "fallback_query": os.getenv("FALLBACK_QUERY", "kurzgesagt"),
    "fallback_max_results": int(os.getenv("FALLBACK_MAX_RESULTS", "5")),
    "fallback_cache_ttl_sec": int(os.getenv("FALLBACK_CACHE_TTL_SEC", "3600")),
}

logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"].upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Simple in-memory cache for fallback queries (to avoid repeated yt-dlp calls)
_FALLBACK_CACHE = {"ts": 0.0, "videos": []}


def search_youtube(query: str, max_results: int = 20):
    """Search YouTube and return a list of flat video results."""
    if not HAS_YTDLP:
        logger.error("yt-dlp not available")
        return []

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "force_generic_extractor": False,
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

                    vid = entry.get("id", "")
                    videos.append({
                        "id": vid,
                        "title": entry.get("title", "Unknown"),
                        "url": entry.get("url") or f"https://www.youtube.com/watch?v={vid}",
                        "channel": entry.get("channel", entry.get("uploader", "Unknown")),
                        "duration": entry.get("duration", 0),
                        "view_count": entry.get("view_count", 0),
                        "upload_date": entry.get("upload_date", ""),
                        "description": entry.get("description", ""),
                    })

            return videos
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
        return []


def get_fallback_videos():
    """Return cached fallback results (refreshing when TTL expires)."""
    now = time.time()
    if _FALLBACK_CACHE["videos"] and (now - _FALLBACK_CACHE["ts"] < CONFIG["fallback_cache_ttl_sec"]):
        return _FALLBACK_CACHE["videos"]

    q = CONFIG["fallback_query"]
    logger.info(f"Fallback search (for empty query): {q}")
    vids = search_youtube(q, max_results=CONFIG["fallback_max_results"])
    _FALLBACK_CACHE["ts"] = now
    _FALLBACK_CACHE["videos"] = vids
    return vids


def generate_guid(video_id: str):
    """Generate a unique GUID for a result."""
    return hashlib.md5(video_id.encode("utf-8")).hexdigest()


def format_torznab_xml(videos, query: str = ""):
    """Format search results as Torznab XML (RSS)."""
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

    # Atom self link (best-effort)
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
        guid.text = generate_guid(video.get("id", ""))

        link = SubElement(item, "link")
        link.text = video.get("url", "")

        comments = SubElement(item, "comments")
        comments.text = f"Channel: {video.get('channel', 'Unknown')}"

        # Publication date
        upload_date = video.get("upload_date", "")
        if upload_date:
            try:
                pub_date = datetime.strptime(upload_date, "%Y%m%d")
                pub_date_elem = SubElement(item, "pubDate")
                pub_date_elem.text = pub_date.strftime("%a, %d %b %Y %H:%M:%S +0000")
            except Exception:
                pass

        # Size estimate: ~5MB per minute (roughly 720p-ish)
        duration = video.get("duration") or 600
        duration_mins = float(duration) / 60.0
        estimated_size = int(duration_mins * 5 * 1024 * 1024)

        size = SubElement(item, "size")
        size.text = str(estimated_size)

        # Category (TV)
        category = SubElement(item, "category")
        category.text = "5000"

        # Enclosure (Sonarr expects torrent-like)
        SubElement(item, "enclosure", {
            "url": video.get("url", ""),
            "length": str(estimated_size),
            "type": "application/x-bittorrent",
        })

        # Torznab attributes
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "category", "value": "5000"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "seeders", "value": "100"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "peers", "value": "100"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "downloadvolumefactor", "value": "0"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "uploadvolumefactor", "value": "1"})

    return tostring(rss, encoding="unicode")


def get_capabilities_xml():
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


class TorznabHandler(BaseHTTPRequestHandler):
    """HTTP handler for Torznab API."""

    def log_message(self, fmt, *args):
        logger.debug("HTTP: " + (fmt % args))

    def _send_xml(self, xml_content: str):
        """Send XML response."""
        body = xml_content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str):
        """Send Torznab-style error response (still HTTP 200)."""
        error_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<error code="{code}" description="{message}"/>'
        )
        self._send_xml(error_xml)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # Serve only /api (and optionally / as a convenience)
        if parsed.path not in ("/api", "/"):
            self._send_error(201, f"Unknown path: {parsed.path}")
            return

        params = urllib.parse.parse_qs(parsed.query)

        # Get API key
        apikey = params.get("apikey", [""])[0]

        # Check API key
        if CONFIG["api_key"] and apikey != CONFIG["api_key"]:
            logger.warning(f"Invalid API key: {apikey}")
            self._send_error(100, "Invalid API Key")
            return

        # Action (t parameter)
        action = params.get("t", [""])[0].lower()
        logger.info(f"Request: action={action}, params={params}")

        # If t is omitted, behave like caps (many clients probe this way)
        if action in ("", "caps"):
            xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + get_capabilities_xml()
            self._send_xml(xml)
            return

        if action in ("search", "tvsearch"):
            # Base query
            q = params.get("q", [""])[0].strip()

            # NOTE: season/ep are intentionally ignored for YouTube (natural language works better)
            # season = params.get("season", [""])[0]
            # ep = params.get("ep", [""])[0]

            if not q:
                # Fix: Prowlarr "Test" calls t=search with no q.
                # Return fallback results so the test returns items and can be saved.
                videos = get_fallback_videos()
                xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(videos, CONFIG["fallback_query"])
                self._send_xml(xml)
                return

            logger.info(f"Searching YouTube for: {q}")
            videos = search_youtube(q)
            logger.info(f"Found {len(videos)} results")

            xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(videos, q)
            self._send_xml(xml)
            return

        if action == "download":
            # Redirect to YouTube URL if called; normally enclosure URL is the YouTube URL
            link = params.get("link", params.get("id", [""]))[0]
            if link:
                self.send_response(302)
                self.send_header("Location", link)
                self.end_headers()
            else:
                self._send_error(200, "Missing download link")
            return

        self._send_error(201, f"Unknown action: {action}")


def main():
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         YouTube Indexer for Prowlarr/Sonarr                  ║
║         Torznab-compatible API Server                        ║
║                                                              ║
║              Created by Ioannis Kokkinis                     ║
╠══════════════════════════════════════════════════════════════╣
║  Add this to Prowlarr as a Generic Torznab indexer:          ║
║                                                              ║
║  URL: http://localhost:{CONFIG["port"]}                      ║
║  API Path: /api                                              ║
║  API Key: {CONFIG["api_key"]}                                ║
║                                                              ║
║  Note: empty t=search calls return fallback results          ║
║  to satisfy Prowlarr "Test".                                 ║
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
