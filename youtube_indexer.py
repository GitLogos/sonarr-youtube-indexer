#!/usr/bin/env python3
"""
YouTube Indexer for Sonarr (v6.0 - Final)
=========================================
- Sonarr API Integration: Uses seriesid to get exact titles/languages.
- Zero-Dependency: Uses built-in urllib for all API calls.
- Empty Query Protection: Handles Prowlarr 'Test' and 'Recent' searches.
- Enhanced Language: Detects auto-generated captions (a.en, en-orig).
- Fixed Sorting: Resolved the 'dict vs dict' comparison crash.
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

# Check for yt-dlp
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
    "sonarr_url": os.getenv("SONARR_URL", "http://localhost:8989"),
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
    if not CONFIG["sonarr_api_key"]:
        return None
    url = f"{CONFIG['sonarr_url'].rstrip('/')}/api/v3/{endpoint}"
    params = params or {}
    params["apikey"] = CONFIG["sonarr_api_key"]
    try:
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(full_url, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        logger.error(f"Sonarr API connection error: {e}")
        return None

def get_sonarr_metadata(series_id, season, episode):
    """Retrieves episode title and series language from Sonarr."""
    if not series_id:
        return None, "en"

    # 1. Determine Series Language
    series = sonarr_api_get(f"series/{series_id}")
    series_lang = "en"
    if series:
        lang_data = series.get('languageProfile') or series.get('language')
        if isinstance(lang_data, dict):
            series_lang = lang_data.get('name', 'en').lower()[:2]
        elif isinstance(lang_data, str):
            series_lang = lang_data.lower()[:2]
    
    # 2. Determine Episode Title
    episodes = sonarr_api_get("episode", {"seriesId": series_id})
    if episodes:
        for ep in episodes:
            if str(ep.get('seasonNumber')) == str(season) and str(ep.get('episodeNumber')) == str(episode):
                return ep.get('title'), series_lang

    return None, series_lang

# -----------------------------------------------------------------------------
# Metadata Extraction & Quality
# -----------------------------------------------------------------------------

def get_inferred_language(entry):
    # Prefer explicit audio language metadata
    lang = entry.get("audio_language") or entry.get("language")
    if lang and lang != 'und':
        return lang.split('-')[0]
    
    # Fallback to scanning subtitle/caption tags (a.en, en-US, etc)
    for key in ["subtitles", "automatic_captions"]:
        subs = entry.get(key) or {}
        if subs:
            raw_key = list(subs.keys())[0]
            return raw_key.replace('a.', '').split('-')[0]
    return "en"

def get_video_quality(entry):
    h = entry.get("height") or 0
    if h >= 2160: return "2160p"
    if h >= 1080: return "1080p"
    if h >= 720:  return "720p"
    return "480p"

def score_video(video, show_name, ep_title):
    score = 0
    title = video.get("title", "").lower()
    channel = video.get("channel", "").lower()
    
    if ep_title and ep_title.lower() in title: score += 150
    if show_name.lower() in channel: score += 80
    if show_name.lower() in title: score += 50
    
    # High quality bonus
    if video.get('height') and video.get('height') >= 1080:
        score += 20

    # Penalties for fluff content
    if any(x in title for x in ['reaction', 'review', 'trailer', 'teaser', 'clips']):
        score -= 400
    return score

# -----------------------------------------------------------------------------
# Core Search Logic
# -----------------------------------------------------------------------------

def search_youtube(query, series_id, season, ep):
    if not query or query.strip() == "":
        logger.info("Empty query received (likely Prowlarr test). Skipping.")
        return []

    ep_title, target_lang = get_sonarr_metadata(series_id, season, ep)
    
    # Search string strategy
    if ep_title:
        search_str = f'"{query}" "{ep_title}"'
    else:
        search_str = f'"{query}" S{int(season or 1):02d}E{int(ep or 1):02d}'
    
    logger.info(f"Processing Search: {search_str} (Language: {target_lang})")

    candidates = []
    # STAGE 1: Fast scan (No JS runtime needed here)
    fast_opts = {"quiet": True, "extract_flat": "in_playlist", "ignoreerrors": True}
    
    try:
        with yt_dlp.YoutubeDL(fast_opts) as ydl:
            res = ydl.extract_info(f"ytsearch10:{search_str}", download=False)
            for entry in res.get("entries", []):
                if not entry: continue
                if entry.get("duration") and entry.get("duration") < CONFIG["min_duration"]:
                    continue
                score = score_video(entry, query, ep_title)
                if score > 0:
                    # Storing only essential info to avoid sorting dicts
                    candidates.append((score, entry))

        # STAGE 2: Deep Scrape (Uses Node.js/FFmpeg for full metadata)
        final_results = []
        # Fix: Sort by score (index 0) ONLY to avoid TypeError
        top_candidates = sorted(candidates, key=lambda x: x[0], reverse=True)[:3]

        deep_opts = {
            "quiet": True, 
            "extract_flat": False, 
            "skip_download": True, 
            "ignoreerrors": True, 
            "geo_bypass": True,
            "writesubtitles": True,
            "writeautomaticsub": True
        }

        with yt_dlp.YoutubeDL(deep_opts) as ydl:
            for score, fast_entry in top_candidates:
                try:
                    video_url = fast_entry.get('url') or f"https://www.youtube.com/watch?v={fast_entry['id']}"
                    info = ydl.extract_info(video_url, download=False)
                    if not info: continue
                    
                    vid_lang = get_inferred_language(info)
                    # Significant boost for language match
                    final_score = score + (100 if vid_lang == target_lang else 0)

                    final_results.append({
                        "id": info['id'],
                        "title": info['title'],
                        "url": info.get('webpage_url', video_url),
                        "language": vid_lang,
                        "quality": get_video_quality(info),
                        "score": final_score,
                        "duration": info.get("duration", 0),
                        "upload_date": info.get("upload_date", "")
                    })
                except Exception as e:
                    logger.debug(f"Stage 2 error for {video_url}: {e}")
                    continue
            
        return sorted(final_results, key=lambda x: x['score'], reverse=True)

    except Exception as e:
        logger.error(f"Search failure: {e}")
        return []

# -----------------------------------------------------------------------------
# Torznab Interface
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
        
        # Calculate size for Sonarr's parser
        duration_mins = (video.get('duration') or 0) / 60
        mb_per_min = 15 if "2160" in q else 8
        size_bytes = int(duration_mins * mb_per_min * 1024 * 1024)
        
        SubElement(item, "size").text = str(size_bytes)
        SubElement(item, "enclosure", {"url": video['url'], "length": str(size_bytes), "type": "application/x-bittorrent"})
        
        # Torznab Attributes
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "language", "value": video['language']})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "category", "value": "5000"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "seeders", "value": "100"})

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
            body = (
                '<caps><server title="YouTube"/><searching>'
                '<search available="yes" supportedParams="q"/>'
                '<tv-search available="yes" supportedParams="q,season,ep,seriesid"/>'
                '</searching></caps>'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

if __name__ == "__main__":
    if not HAS_YTDLP:
        print("CRITICAL: yt-dlp module not found. Check Dockerfile installation.")
    else:
        logger.info(f"YouTube Indexer started on port {CONFIG['port']}")
        HTTPServer((CONFIG["host"], CONFIG["port"]), TorznabHandler).serve_forever()
