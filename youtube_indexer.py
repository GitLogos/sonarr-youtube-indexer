#!/usr/bin/env python3
"""
YouTube Indexer for Prowlarr/Sonarr
====================================
Features:
- TMDB Integration: Resolves SxxExx to actual Episode Titles for better searching.
- Deep Metadata: Scrapes captions to accurately infer language.
- Quality Mapping: Detects resolution and formats titles for Sonarr profiles.
- Smart Scoring: Ranks official-style uploads higher than fan/review content.

Author: Ioannis Kokkinis
"""

import os
import re
import hashlib
import urllib.parse
import time
import requests
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

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
CONFIG = {
    "host": os.getenv("HOST", "0.0.0.0"),
    "port": int(os.getenv("PORT", "9117")),
    "api_key": os.getenv("API_KEY", "youtubeindexer"),
    "tmdb_api_key": os.getenv("TMDB_API_KEY", ""), # Set this in Docker Compose
    "indexer_name": os.getenv("INDEXER_NAME", "YouTube"),
    "log_level": os.getenv("LOG_LEVEL", "INFO"),
    "min_duration": int(os.getenv("MIN_DURATION", "300")), # 5 mins
}

logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"].upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Metadata & Scoring Helpers
# -----------------------------------------------------------------------------

def get_episode_title_from_tmdb(show_name, season, ep):
    """Fetches the actual episode name from TMDB to improve YouTube search."""
    if not CONFIG["tmdb_api_key"]:
        return None
    
    try:
        # 1. Search for the TV Show ID
        search_url = f"https://api.themoviedb.org/3/search/tv"
        params = {"api_key": CONFIG["tmdb_api_key"], "query": show_name}
        search_res = requests.get(search_url, params=params, timeout=5).json()
        
        if not search_res.get('results'):
            return None
        
        series_id = search_res['results'][0]['id']
        
        # 2. Get the specific Episode Title
        ep_url = f"https://api.themoviedb.org/3/tv/{series_id}/season/{season}/episode/{ep}"
        ep_params = {"api_key": CONFIG["tmdb_api_key"]}
        ep_res = requests.get(ep_url, params=ep_params, timeout=5).json()
        
        return ep_res.get('name')
    except Exception as e:
        logger.error(f"TMDB Lookup Error: {e}")
        return None

def get_inferred_language(entry):
    """Infers spoken language via subtitles/captions or metadata."""
    subs = entry.get("subtitles") or {}
    auto = entry.get("automatic_captions") or {}
    
    if subs: return list(subs.keys())[0].split('-')[0]
    if auto: return list(auto.keys())[0].split('-')[0]
    
    meta_lang = entry.get("language")
    return meta_lang if (meta_lang and meta_lang != 'und') else "en"

def get_video_quality(entry):
    """Maps vertical resolution to standard quality tags."""
    h = entry.get("height") or 0
    if h >= 2160: return "2160p"
    if h >= 1440: return "1440p"
    if h >= 1080: return "1080p"
    if h >= 720:  return "720p"
    return "480p"

def score_video(video, show_name, ep_title, season, ep):
    """Ranks results based on relevance."""
    score = 0
    title = video.get("title", "").lower()
    channel = video.get("channel", "").lower()
    show_name = show_name.lower()
    
    # Priority 1: Episode Title match (Highest)
    if ep_title and ep_title.lower() in title:
        score += 150
    
    # Priority 2: Show Name in Channel (Authenticity)
    if show_name in channel:
        score += 80
        
    # Priority 3: Show Name in Title
    if show_name in title:
        score += 50

    # Priority 4: SxxExx or Ep match
    ep_num = int(ep)
    if any(re.search(p, title) for p in [rf's{int(season):02d}e{ep_num:02d}', rf'ep\s*{ep_num}']):
        score += 30

    # Penalties
    if any(x in title for x in ['reaction', 'review', 'trailer', 'teaser']):
        score -= 300

    return score

# -----------------------------------------------------------------------------
# Core Search Logic
# -----------------------------------------------------------------------------

def search_youtube(query: str, season: str = "", ep: str = "", max_results: int = 10):
    if not HAS_YTDLP: return []

    # 1. Enhance query with TMDB if possible
    ep_title = None
    if season and ep:
        ep_title = get_episode_title_from_tmdb(query, season, ep)
    
    if ep_title:
        search_str = f'"{query}" "{ep_title}"'
        logger.info(f"Using TMDB Enhanced Search: {search_str}")
    else:
        search_str = f'"{query}" S{int(season or 1):02d}E{int(ep or 1):02d}'

    # 2. Configure yt-dlp for Deep Scrape (extract_flat=False)
    ydl_opts = {
        "quiet": True, "no_warnings": True, "extract_flat": False, "skip_download": True,
        "writesubtitles": True, "writeautomaticsub": True,
        "fields": ["id", "title", "url", "channel", "duration", "upload_date", "language", "subtitles", "automatic_captions", "height"]
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(f"ytsearch{max_results}:{search_str}", download=False)
            videos = []
            for entry in result.get("entries", []):
                if not entry or (entry.get("duration") or 0) < CONFIG["min_duration"]:
                    continue
                
                entry_score = score_video(entry, query, ep_title, season, ep)
                if entry_score > 0:
                    videos.append({
                        "id": entry.get("id"),
                        "title": entry.get("title"),
                        "url": entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id')}",
                        "channel": entry.get("channel", "Unknown"),
                        "duration": entry.get("duration", 0),
                        "upload_date": entry.get("upload_date", ""),
                        "language": get_inferred_language(entry),
                        "quality": get_video_quality(entry),
                        "score": entry_score
                    })
            return sorted(videos, key=lambda x: x['score'], reverse=True)
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

# -----------------------------------------------------------------------------
# XML Generation & Server
# -----------------------------------------------------------------------------

def format_torznab_xml(videos):
    rss = Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = CONFIG["indexer_name"]

    for video in videos:
        item = SubElement(channel, "item")
        quality = video.get("quality", "1080p")
        # Sonarr parses the title string for quality tags
        SubElement(item, "title").text = f"{video.get('title')} [{quality} WEBDL]"
        SubElement(item, "guid").text = hashlib.md5(video['id'].encode()).hexdigest()
        SubElement(item, "link").text = video['url']
        
        # pubDate for 'Age' column
        date_str = video.get("upload_date")
        dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc) if date_str else datetime.now(timezone.utc)
        SubElement(item, "pubDate").text = dt.strftime("%a, %d %b %Y %H:%M:%S %z")

        # Torznab Attributes
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "language", "value": video['language']})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "category", "value": "5000"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "seeders", "value": "100"})
        
        # Estimated Size
        duration_mins = video.get("duration", 0) / 60
        mb_per_min = 15 if "2160" in quality else 8
        size_bytes = int(duration_mins * mb_per_min * 1024 * 1024)
        SubElement(item, "size").text = str(size_bytes)
        SubElement(item, "enclosure", {"url": video['url'], "length": str(size_bytes), "type": "application/x-bittorrent"})

    return tostring(rss, encoding="unicode")

class TorznabHandler(BaseHTTPRequestHandler):
    def _send_xml(self, xml_content):
        body = xml_content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        action = params.get("t", [""])[0].lower()

        if action in ("search", "tvsearch"):
            q = params.get("q", [""])[0]
            s = params.get("season", [""])[0]
            e = params.get("ep", [""])[0]
            videos = search_youtube(q, s, e)
            self._send_xml('<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(videos))
        elif action == "caps":
            self._send_xml('<caps><server title="YouTube"/><searching><search available="yes" supportedParams="q"/><tv-search available="yes" supportedParams="q,season,ep"/></searching></caps>')

def main():
    if not HAS_YTDLP:
        print("Install yt-dlp first.")
        return
    server = HTTPServer((CONFIG["host"], CONFIG["port"]), TorznabHandler)
    logger.info(f"Starting server on {CONFIG['port']}...")
    server.serve_forever()

if __name__ == "__main__":
    main()
