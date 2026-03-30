#!/usr/bin/env python3
"""
YouTube Indexer for Prowlarr/Sonarr
====================================
A Torznab-compatible indexer that searches YouTube and returns results
that Sonarr can use to trigger downloads via the YouTube download client.

This bridges the gap between Sonarr's episode metadata (from TheTVDB) and
actual YouTube video URLs.

Author: Ioannis Kokkinis (Updated with Smart Scoring & Filtering)
License: MIT
"""

import os
import re
import hashlib
import urllib.parse
import time
from datetime import datetime, timezone
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

    "fallback_query": os.getenv("FALLBACK_QUERY", "kurzgesagt"),
    "fallback_max_results": int(os.getenv("FALLBACK_MAX_RESULTS", "5")),
    "fallback_cache_ttl_sec": int(os.getenv("FALLBACK_CACHE_TTL_SEC", "3600")),
    
    # NEW: Minimum duration in seconds (e.g., 300 = 5 minutes) to filter out clips/trailers
    "min_duration": int(os.getenv("MIN_DURATION", "300")),
}

logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"].upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

_FALLBACK_CACHE = {"ts": 0.0, "videos": []}


def score_video(video, query: str, season: str, ep: str):
    """
    Ranks videos based on matching priority to filter out irrelevant YouTube junk.
    """
    score = 0
    title = video.get("title", "").lower()
    channel = video.get("channel", "").lower()
    query_lower = query.lower()
    
    # 1. Show Name in Title (Highest Priority)
    if query_lower and query_lower in title:
        score += 100
        
    # 2. Show Name in Channel Name (Authenticity indicator)
    if query_lower and query_lower in channel:
        score += 50

    # 3. Flexible Episode Matching
    if ep:
        try:
            ep_num = int(ep)
            ep_patterns = [
                rf'episode\s*{ep_num}',
                rf'ep\s*{ep_num}',
                rf'\b{ep_num:02d}\b',
                rf'\b{ep_num}\b'
            ]
            if any(re.search(p, title) for p in ep_patterns):
                score += 40
        except ValueError:
            pass

    # 4. S01E05 format (Lowest Priority / Bonus)
    if season and ep:
        try:
            se_patterns = [
                rf's{int(season):02d}e{int(ep):02d}',
                rf'{int(season)}x{int(ep):02d}'
            ]
            if any(re.search(p, title) for p in se_patterns):
                score += 10
        except ValueError:
            pass

    # 5. Language cleanup (Boost if 'en' or 'eng' is in title/channel)
    if any(lang in title or lang in channel for lang in ['en', 'eng', 'english']):
        score += 5

    # Penalty for junk
    if any(x in title for x in ['reaction', 'review', 'trailer', 'teaser', 'promo']):
        score -= 200

    return score


def search_youtube(query: str, season: str = "", ep: str = "", max_results: int = 25):
    """Search YouTube and return a scored, filtered list of flat video results."""
    if not HAS_YTDLP:
        logger.error("yt-dlp not available")
        return []

    # Construct a broad query for YouTube's engine
    search_str = query
    if season and ep:
        try:
            search_str = f"{query} S{int(season):02d}E{int(ep):02d}"
        except ValueError:
            search_str = f"{query} {season} {ep}"

    # Use extract_flat with specific fields for speed
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "force_generic_extractor": False,
        "fields": ["id", "title", "url", "channel", "uploader", "duration", "view_count", "upload_date", "language"]
    }

    search_url = f"ytsearch{max_results}:{search_str}"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(search_url, download=False)

            videos = []
            if result and "entries" in result:
                for entry in result["entries"]:
                    if not entry:
                        continue
                    
                    # --- DURATION GATEKEEPER ---
                    vid_duration = entry.get("duration") or 0
                    if vid_duration < CONFIG["min_duration"]:
                        logger.debug(f"Skipping '{entry.get('title')}' - too short ({vid_duration}s)")
                        continue
                    
                    # Calculate ranking score
                    entry_score = score_video(entry, query, season, ep)
                    
                    # Only keep videos that have a positive score
                    if entry_score > 0:
                        vid = entry.get("id", "")
                        videos.append({
                            "id": vid,
                            "title": entry.get("title", "Unknown"),
                            "url": entry.get("url") or f"https://www.youtube.com/watch?v={vid}",
                            "channel": entry.get("channel", entry.get("uploader", "Unknown")),
                            "duration": vid_duration,
                            "view_count": entry.get("view_count", 0),
                            "upload_date": entry.get("upload_date", ""),
                            "language": entry.get("language") or "en",
                            "score": entry_score
                        })

            # Sort by score descending
            return sorted(videos, key=lambda x: x['score'], reverse=True)
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


def _rfc822(dt: datetime) -> str:
    """Format a datetime as RFC-822 / RFC-1123 string for RSS pubDate."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


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
        comments.text = f"Channel: {video.get('channel', 'Unknown')} (Score: {video.get('score', 0)})"

        # pubDate (Determines "Age" in Prowlarr/Sonarr)
        upload_date = (video.get("upload_date") or "").strip()
        if upload_date:
            try:
                pub_dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            except Exception:
                pub_dt = datetime.now(timezone.utc)
        else:
            pub_dt = datetime.now(timezone.utc)

        pub_date_elem = SubElement(item, "pubDate")
        pub_date_elem.text = _rfc822(pub_dt)

        # Size estimate: ~5MB per minute (roughly 720p-ish)
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

        # Torznab attributes (Including Language)
        lang = video.get("language", "en")
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "language", "value": lang})
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
    SubElement(caps, "limits", {"max": "100", "default": "25"})

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

        if parsed.path not in ("/api", "/"):
            self._send_error(201, f"Unknown path: {parsed.path}")
            return

        params = urllib.parse.parse_qs(parsed.query)

        apikey = params.get("apikey", [""])[0]
        if CONFIG["api_key"] and apikey != CONFIG["api_key"]:
            logger.warning(f"Invalid API key: {apikey}")
            self._send_error(100, "Invalid API Key")
            return

        action = params.get("t", [""])[0].lower()
        logger.info(f"Request: action={action}, params={params}")

        if action in ("", "caps"):
            xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + get_capabilities_xml()
            self._send_xml(xml)
            return

        if action in ("search", "tvsearch"):
            q = params.get("q", [""])[0].strip()
            season = params.get("season", [""])[0]
            ep = params.get("ep", [""])[0]

            if not q:
                videos = get_fallback_videos()
                xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(videos, CONFIG["fallback_query"])
                self._send_xml(xml)
                return

            logger.info(f"Searching YouTube for: {q} S{season}E{ep}")
            videos = search_youtube(q, season, ep)
            logger.info(f"Found {len(videos)} scored results")

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


def main():
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         YouTube Indexer for Prowlarr/Sonarr                  ║
║         Torznab-compatible API Server (Smart Filtered)       ║
║                                                              ║
║         Created by Ioannis Kokkinis                          ║
╠══════════════════════════════════════════════════════════════╣
║  Add this to Prowlarr as a Generic Torznab indexer:          ║
║                                                              ║
║  URL: http://localhost:{CONFIG["port"]}                                ║
║  API Path: /api                                              ║
║  API Key: {CONFIG["api_key"]}                                  ║
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
