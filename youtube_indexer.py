#!/usr/bin/env python3
import os
import hashlib
import urllib.parse
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from xml.etree.ElementTree import Element, SubElement, tostring
import logging

# Try to import yt-dlp
try:
    import yt_dlp
    HAS_YTDLP = True
except ImportError:
    HAS_YTDLP = False
    print("WARNING: yt-dlp not installed. Install with: pip install yt-dlp")

CONFIG = {
    "host": os.getenv("HOST", "0.0.0.0"),
    "port": int(os.getenv("PORT", "9117")),
    "api_key": os.getenv("API_KEY", "youtubeindexer"),
    "indexer_name": os.getenv("INDEXER_NAME", "YouTube"),
    "log_level": os.getenv("LOG_LEVEL", "INFO"),
    "fallback_query": os.getenv("FALLBACK_QUERY", "kurzgesagt"),
    "fallback_max_results": int(os.getenv("FALLBACK_MAX_RESULTS", "5")),
    "fallback_cache_ttl_sec": int(os.getenv("FALLBACK_CACHE_TTL_SEC", "3600")),
}

logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"].upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

_FALLBACK_CACHE = {"ts": 0.0, "videos": []}

def search_youtube(query: str, max_results: int = 20):
    """Search YouTube and return a list of video results with specific fields."""
    if not HAS_YTDLP:
        logger.error("yt-dlp not available")
        return []

    # CHANGE 1: Efficiently select only the fields we need
    # This prevents yt-dlp from fetching unnecessary data, keeping the indexer fast.
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist", # Allows getting basic metadata without a full webpage scrape
        "force_generic_extractor": False,
        # Surgical selection of fields for the Python API
        "fields": ["id", "title", "url", "channel", "uploader", "duration", "view_count", "upload_date", "description", "language", "entries"]
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
                        # CHANGE 2: Capture the language field
                        "language": entry.get("language") or "en", # Default to 'en' if null
                    })

            return videos
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
        return []

# ... [get_fallback_videos, generate_guid, _rfc822 functions remain the same] ...

def format_torznab_xml(videos, query: str = ""):
    """Format search results as Torznab XML (RSS) including Language and Age."""
    rss = Element("rss", {
        "version": "2.0",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
        "xmlns:torznab": "http://torznab.com/schemas/2015/feed",
    })

    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = CONFIG["indexer_name"]
    SubElement(channel, "description").text = "YouTube Video Indexer for Sonarr"

    for video in videos:
        item = SubElement(channel, "item")
        SubElement(item, "title").text = video.get("title", "Unknown")
        SubElement(item, "guid").text = generate_guid(video.get("id", ""))
        SubElement(item, "link").text = video.get("url", "")
        SubElement(item, "comments").text = f"Channel: {video.get('channel', 'Unknown')}"

        # pubDate (Handles the "Age" column in Prowlarr/Sonarr)
        upload_date = (video.get("upload_date") or "").strip()
        if upload_date:
            try:
                pub_dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            except Exception:
                pub_dt = datetime.now(timezone.utc)
        else:
            pub_dt = datetime.now(timezone.utc)

        SubElement(item, "pubDate").text = _rfc822(pub_dt)

        # Enclosure and Size
        duration = video.get("duration") or 600
        estimated_size = int((float(duration) / 60.0) * 5 * 1024 * 1024)
        SubElement(item, "size").text = str(estimated_size)
        SubElement(item, "enclosure", {
            "url": video.get("url", ""),
            "length": str(estimated_size),
            "type": "application/x-bittorrent",
        })

        # CHANGE 3: Torznab attributes for Language
        # Torznab uses "language" or "languageid" attributes.
        lang = video.get("language", "en")
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "language", "value": lang})
        
        # Standard Torznab attributes
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "category", "value": "5000"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "seeders", "value": "100"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "peers", "value": "100"})

    return tostring(rss, encoding="unicode")

# ... [Rest of the file remains the same: get_capabilities_xml, TorznabHandler, main] ...
