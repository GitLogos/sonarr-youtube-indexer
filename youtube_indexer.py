#!/usr/bin/env python3
"""
YouTube Indexer for Prowlarr/Sonarr
====================================
A Torznab-compatible indexer that searches YouTube and returns results
that Sonarr can use to trigger downloads via the YouTube download client.

Features:
- Smart Scoring: Prioritizes official channel uploads and exact title matches.
- Duration Filter: Blocks clips and trailers.
- Deep Metadata Scrape: Uses captions to accurately infer language.
- Quality Reporting: Appends resolution tags for Sonarr profiles.

Author: Ioannis Kokkinis 
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
# Configuration
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
    
    "min_duration": int(os.getenv("MIN_DURATION", "300")), # 5 minutes
}

logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"].upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

_FALLBACK_CACHE = {"ts": 0.0, "videos": []}


def get_inferred_language(entry):
    """
    Heuristic to determine the spoken language using deep metadata.
    Priority: Manual Subtitles > Automatic Captions > Metadata > Default (en)
    """
    # 1. Manual Subtitles (Created by a human - very reliable)
    subs = entry.get("subtitles")
    if subs:
        return list(subs.keys())[0].split('-')[0]

    # 2. Automatic Captions (YouTube AI - reliable proxy for spoken lang)
    auto_subs = entry.get("automatic_captions")
    if auto_subs:
        return list(auto_subs.keys())[0].split('-')[0]

    # 3. Explicitly tagged language
    meta_lang = entry.get("language")
    if meta_lang and meta_lang != 'und':
        return meta_lang

    return "en" # Fallback


def get_video_quality(entry):
    """
    Extracts the resolution height and formats it for Sonarr.
    YouTube dynamically serves various formats, but yt-dlp usually reports the best available height.
    """
    height = entry.get("height")
    if height:
        if height >= 2160: return "2160p"
        if height >= 1440: return "1440p"
        if height >= 1080: return "1080p"
        if height >= 720:  return "720p"
        if height >= 480:  return "480p"
    
    return "1080p" # Assume 1080p if unable to determine


def score_video(video, query: str, season: str, ep: str):
    """Ranks videos based on matching priority."""
    score = 0
    title = video.get("title", "").lower()
    channel = video.get("channel", "").lower()
    query_lower = query.lower()
    
    if query_lower and query_lower in title:
        score += 100
        
    if query_lower and query_lower in channel:
        score += 50

    if ep:
        try:
            ep_num = int(ep)
            ep_patterns = [
                rf'episode\s*{ep_num}', rf'ep\s*{ep_num}', rf'\b{ep_num:02d}\b', rf'\b{ep_num}\b'
            ]
            if any(re.search(p, title) for p in ep_patterns):
                score += 40
        except ValueError: pass

    if season and ep:
        try:
            se_patterns = [
                rf's{int(season):02d}e{int(ep):02d}', rf'{int(season)}x{int(ep):02d}'
            ]
            if any(re.search(p, title) for p in se_patterns):
                score += 10
        except ValueError: pass

    if any(lang in title or lang in channel for lang in ['en', 'eng', 'english']):
        score += 5

    if any(x in title for x in ['reaction', 'review', 'trailer', 'teaser', 'promo']):
        score -= 200

    return score


def search_youtube(query: str, season: str = "", ep: str = "", max_results: int = 10):
    """Search YouTube with deep metadata extraction for accurate language and quality."""
    if not HAS_YTDLP: return []

    search_str = query
    if season and ep:
        try:
            search_str = f"{query} S{int(season):02d}E{int(ep):02d}"
        except ValueError:
            search_str = f"{query} {season} {ep}"

    # IMPORTANT: extract_flat is False to allow fetching subtitles and resolutions
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False, 
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "fields": [
            "id", "title", "url", "channel", "uploader", "duration", 
            "upload_date", "language", "subtitles", "automatic_captions", "height"
        ]
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # ytsearch limits results. Deep scrape takes ~1s per video.
            result = ydl.extract_info(f"ytsearch{max_results}:{search_str}", download=False)
            videos = []
            
            if result and "entries" in result:
                for entry in result["entries"]:
                    if not entry: continue
                    
                    vid_duration = entry.get("duration") or 0
                    if vid_duration < CONFIG["min_duration"]: continue
                    
                    entry_score = score_video(entry, query, season, ep)
                    
                    if entry_score > 0:
                        vid = entry.get("id", "")
                        resolution = get_video_quality(entry)
                        inferred_lang = get_inferred_language(entry)
                        
                        videos.append({
                            "id": vid,
                            "title": entry.get("title", "Unknown"),
                            "url": entry.get("url") or f"https://www.youtube.com/watch?v={vid}",
                            "channel": entry.get("channel", entry.get("uploader", "Unknown")),
                            "duration": vid_duration,
                            "upload_date": entry.get("upload_date", ""),
                            "language": inferred_lang,
                            "quality": resolution,
                            "score": entry_score
                        })

            return sorted(videos, key=lambda x: x['score'], reverse=True)
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
        return []


def get_fallback_videos():
    now = time.time()
    if _FALLBACK_CACHE["videos"] and (now - _FALLBACK_CACHE["ts"] < CONFIG["fallback_cache_ttl_sec"]):
        return _FALLBACK_CACHE["videos"]

    q = CONFIG["fallback_query"]
    vids = search_youtube(q, max_results=CONFIG["fallback_max_results"])
    _FALLBACK_CACHE["ts"] = now
    _FALLBACK_CACHE["videos"] = vids
    return vids


def generate_guid(video_id: str):
    return hashlib.md5(video_id.encode("utf-8")).hexdigest()


def _rfc822(dt: datetime) -> str:
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def format_torznab_xml(videos, query: str = ""):
    rss = Element("rss", {
        "version": "2.0",
        "xmlns:atom": "http://www.w3.org/2005/Atom",
        "xmlns:torznab": "http://torznab.com/schemas/2015/feed",
    })

    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = CONFIG["indexer_name"]
    SubElement(channel, "description").text = "YouTube Video Indexer for Sonarr"
    SubElement(channel, "{http://www.w3.org/2005/Atom}link", {
        "href": f"http://localhost:{CONFIG['port']}/api", "rel": "self", "type": "application/rss+xml",
    })

    for video in videos:
        item = SubElement(channel, "item")

        # Inject Quality into the Title for Sonarr Parsing
        clean_title = video.get("title", "Unknown")
        quality_tag = video.get("quality", "1080p")
        formatted_title = f"{clean_title} [{quality_tag} WEBDL]"
        SubElement(item, "title").text = formatted_title

        SubElement(item, "guid").text = generate_guid(video.get("id", ""))
        SubElement(item, "link").text = video.get("url", "")
        SubElement(item, "comments").text = f"Channel: {video.get('channel')} (Score: {video.get('score')})"

        upload_date = (video.get("upload_date") or "").strip()
        if upload_date:
            try: pub_dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
            except Exception: pub_dt = datetime.now(timezone.utc)
        else:
            pub_dt = datetime.now(timezone.utc)

        SubElement(item, "pubDate").text = _rfc822(pub_dt)

        # Scale estimated size based on reported quality
        duration_mins = float(video.get("duration", 600)) / 60.0
        mb_per_min = 15 if quality_tag in ["2160p", "1440p"] else (8 if quality_tag == "1080p" else 4)
        estimated_size = int(duration_mins * mb_per_min * 1024 * 1024)

        SubElement(item, "size").text = str(estimated_size)
        SubElement(item, "category").text = "5000"

        SubElement(item, "enclosure", {
            "url": video.get("url", ""),
            "length": str(estimated_size),
            "type": "application/x-bittorrent",
        })

        lang = video.get("language", "en")
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "language", "value": lang})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "category", "value": "5000"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "seeders", "value": "100"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "peers", "value": "100"})

    return tostring(rss, encoding="unicode")


def get_capabilities_xml():
    caps = Element("caps")
    SubElement(caps, "server", {"version": "1.0", "title": CONFIG["indexer_name"]})
    SubElement(caps, "limits", {"max": "100", "default": "10"})

    searching = SubElement(caps, "searching")
    SubElement(searching, "search", {"available": "yes", "supportedParams": "q"})
    SubElement(searching, "tv-search", {"available": "yes", "supportedParams": "q,season,ep"})

    categories = SubElement(caps, "categories")
    cat = SubElement(categories, "category", {"id": "5000", "name": "TV"})
    SubElement(cat, "subcat", {"id": "5030", "name": "TV/SD"})
    SubElement(cat, "subcat", {"id": "5040", "name": "TV/HD"})
    SubElement(cat, "subcat", {"id": "5045", "name": "TV/UHD"})

    return tostring(caps, encoding="unicode")


class TorznabHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): logger.debug("HTTP: " + (fmt % args))

    def _send_xml(self, xml_content: str):
        body = xml_content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str):
        self._send_xml(f'<?xml version="1.0" encoding="UTF-8"?>\n<error code="{code}" description="{message}"/>')

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/api", "/"): return self._send_error(201, f"Unknown path: {parsed.path}")

        params = urllib.parse.parse_qs(parsed.query)
        if CONFIG["api_key"] and params.get("apikey", [""])[0] != CONFIG["api_key"]:
            return self._send_error(100, "Invalid API Key")

        action = params.get("t", [""])[0].lower()

        if action in ("", "caps"):
            return self._send_xml('<?xml version="1.0" encoding="UTF-8"?>\n' + get_capabilities_xml())

        if action in ("search", "tvsearch"):
            q = params.get("q", [""])[0].strip()
            season = params.get("season", [""])[0]
            ep = params.get("ep", [""])[0]

            if not q:
                return self._send_xml('<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(get_fallback_videos(), CONFIG["fallback_query"]))

            videos = search_youtube(q, season, ep)
            return self._send_xml('<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(videos, q))

        if action == "download":
            link = params.get("link", params.get("id", [""]))[0]
            if link:
                self.send_response(302)
                self.send_header("Location", link)
                self.end_headers()
            else: self._send_error(200, "Missing download link")
            return

        self._send_error(201, f"Unknown action: {action}")


def main():
    if not HAS_YTDLP:
        print("ERROR: yt-dlp is required.")
        return

    server = HTTPServer((CONFIG["host"], CONFIG["port"]), TorznabHandler)
    logger.info(f"Starting Torznab server on {CONFIG['host']}:{CONFIG['port']}")
    try: server.serve_forever()
    except KeyboardInterrupt: server.shutdown()


if __name__ == "__main__":
    main()
