#!/usr/bin/env python3
"""
YouTube Indexer for Sonarr (Strategy 1: Sonarr-Native)
=====================================================
Features:
- Sonarr API Integration: Fetches episode titles and languages directly from Sonarr.
- Zero External Dependencies: Uses urllib for API calls.
- Hybrid Search: Fast discovery + Deep metadata scraping for quality/captions.
- Language Enforcement: Prioritizes results matching the Sonarr series language.
"""

import os
import re
import hashlib
import urllib.parse
import urllib.request
import json
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from xml.etree.ElementTree import Element, SubElement, tostring
import logging

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
    "sonarr_url": os.getenv("SONARR_URL", "http://localhost:8989"), # Full URL including http
    "sonarr_api_key": os.getenv("SONARR_API_KEY", ""),
    "indexer_name": os.getenv("INDEXER_NAME", "YouTube"),
    "log_level": os.getenv("LOG_LEVEL", "INFO"),
    "min_duration": int(os.getenv("MIN_DURATION", "300")),
}

logging.basicConfig(
    level=getattr(logging, CONFIG["log_level"].upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Sonarr API Interaction
# -----------------------------------------------------------------------------

def sonarr_api_get(endpoint, params=None):
    """Helper for Sonarr API GET requests."""
    if not CONFIG["sonarr_api_key"]:
        logger.error("SONARR_API_KEY is not set.")
        return None
    
    url = f"{CONFIG['sonarr_url'].rstrip('/')}/api/v3/{endpoint}"
    params = params or {}
    params["apikey"] = CONFIG["sonarr_api_key"]
    
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(full_url, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        logger.error(f"Sonarr API error ({endpoint}): {e}")
        return None

def get_sonarr_metadata(series_id, season, episode):
    """Retrieves episode title and series language from Sonarr."""
    if not series_id: return None, "en"

    # 1. Get Series Language
    series = sonarr_api_get(f"series/{series_id}")
    # Sonarr v3 series object has nested metadata or language tags
    # Defaulting to 'en' if not explicitly found in a way we can parse simply
    series_lang = "en" 
    
    # 2. Get Episode Title
    episodes = sonarr_api_get("episode", {"seriesId": series_id, "seasonNumber": season})
    if episodes:
        for ep in episodes:
            if str(ep.get('episodeNumber')) == str(episode):
                return ep.get('title'), series_lang

    return None, series_lang

# -----------------------------------------------------------------------------
# YouTube Metadata Helpers
# -----------------------------------------------------------------------------

def get_inferred_language(entry):
    # Check audio_language first
    lang = entry.get("audio_language") or entry.get("language")
    if lang and lang != 'und':
        return lang.split('-')[0]

    # Check subtitles/automatic captions
    for key in ["subtitles", "automatic_captions"]:
        subs = entry.get(key) or {}
        if subs:
            return list(subs.keys())[0].replace('a.', '').split('-')[0]
    return "en"

def get_video_quality(entry):
    h = entry.get("height") or 0
    if h >= 2160: return "2160p"
    if h >= 1080: return "1080p"
    if h >= 720:  return "720p"
    return "480p"

def score_video(video, show_name, ep_title, series_lang):
    score = 0
    title = video.get("title", "").lower()
    channel = video.get("channel", "").lower()
    
    # Matching Logic
    if ep_title and ep_title.lower() in title: score += 150
    if show_name.lower() in channel: score += 80
    if show_name.lower() in title: score += 50
    
    # Penalties for noise
    if any(x in title for x in ['reaction', 'review', 'trailer', 'teaser']): score -= 400
    return score

# -----------------------------------------------------------------------------
# Search Logic
# -----------------------------------------------------------------------------

def search_youtube(query, series_id, season, ep):
    # 1. Fetch metadata from Sonarr
    ep_title, target_lang = get_sonarr_metadata(series_id, season, ep)
    
    # 2. Construct Search String
    if ep_title:
        search_str = f'"{query}" "{ep_title}"'
        logger.info(f"Sonarr Match: Searching for '{ep_title}' in {target_lang}")
    else:
        search_str = f'"{query}" S{int(season or 1):02d}E{int(ep or 1):02d}'

    # 3. Stage 1: Fast Search
    candidates = []
    fast_opts = {"quiet": True, "extract_flat": "in_playlist", "ignoreerrors": True}
    with yt_dlp.YoutubeDL(fast_opts) as ydl:
        res = ydl.extract_info(f"ytsearch10:{search_str}", download=False)
        for entry in res.get("entries", []):
            if not entry: continue
            if entry.get("duration") and entry.get("duration") < CONFIG["min_duration"]: continue
            score = score_video(entry, query, ep_title, target_lang)
            if score > 0: candidates.append((score, entry))

    # 4. Stage 2: Deep Scrape (Top 3)
    final_results = []
    deep_opts = {"quiet": True, "extract_flat": False, "skip_download": True, "ignoreerrors": True, "geo_bypass": True}
    with yt_dlp.YoutubeDL(deep_opts) as ydl:
        for score, fast_entry in sorted(candidates, reverse=True)[:3]:
            info = ydl.extract_info(fast_entry['url'], download=False)
            if not info: continue
            
            vid_lang = get_inferred_language(info)
            # Language Bonus
            final_score = score + (100 if vid_lang == target_lang else 0)

            final_results.append({
                "id": info['id'],
                "title": info['title'],
                "url": info.get('webpage_url'),
                "language": vid_lang,
                "quality": get_video_quality(info),
                "score": final_score,
                "duration": info.get("duration", 0),
                "upload_date": info.get("upload_date", "")
            })
    return final_results

# -----------------------------------------------------------------------------
# Web Server
# -----------------------------------------------------------------------------

def format_torznab_xml(videos):
    rss = Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = CONFIG["indexer_name"]

    for video in videos:
        item = SubElement(channel, "item")
        q = video['quality']
        SubElement(item, "title").text = f"{video['title']} [{q} WEBDL]"
        SubElement(item, "guid").text = hashlib.md5(video['id'].encode()).hexdigest()
        SubElement(item, "link").text = video['url']
        
        # Enclosure for Sonarr
        duration_mins = video['duration'] / 60
        size = int(duration_mins * (15 if "2160" in q else 8) * 1024 * 1024)
        SubElement(item, "size").text = str(size)
        SubElement(item, "enclosure", {"url": video['url'], "length": str(size), "type": "application/x-bittorrent"})
        
        # Attributes
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "language", "value": video['language']})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "category", "value": "5000"})

    return tostring(rss, encoding="unicode")

class TorznabHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        action = params.get("t", [""])[0].lower()

        if action in ("search", "tvsearch"):
            q = params.get("q", [""])[0]
            sid = params.get("seriesid", [None])[0]
            s = params.get("season", ["1"])[0]
            e = params.get("ep", ["1"])[0]
            
            videos = search_youtube(q, sid, s, e)
            body = '<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(videos)
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
        elif action == "caps":
            body = '<caps><server title="YouTube"/><searching><tv-search available="yes" supportedParams="q,season,ep,seriesid"/></searching></caps>'
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

if __name__ == "__main__":
    if not HAS_YTDLP:
        print("Install yt-dlp.")
    else:
        HTTPServer((CONFIG["host"], CONFIG["port"]), TorznabHandler).serve_forever()
