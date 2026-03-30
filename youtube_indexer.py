#!/usr/bin/env python3
"""
YouTube Indexer for Prowlarr/Sonarr
====================================
Features:
- TMDB Integration: Resolves SxxExx to actual Episode Titles.
- Hybrid Search: Fast discovery + Deep metadata scraping for the top results.
- Robust Error Handling: Skips "Unavailable" or "Geo-blocked" videos without crashing.
- Language/Quality: Inferred from deep metadata.

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
    "tmdb_api_key": os.getenv("TMDB_API_KEY", ""),
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
# Metadata & Scoring Helpers
# -----------------------------------------------------------------------------

def get_episode_title_from_tmdb(show_name, season, ep):
    if not CONFIG["tmdb_api_key"]: return None
    try:
        search_url = "https://api.themoviedb.org/3/search/tv"
        params = {"api_key": CONFIG["tmdb_api_key"], "query": show_name}
        search_res = requests.get(search_url, params=params, timeout=5).json()
        if not search_res.get('results'): return None
        series_id = search_res['results'][0]['id']
        ep_url = f"https://api.themoviedb.org/3/tv/{series_id}/season/{season}/episode/{ep}"
        ep_res = requests.get(ep_url, params={"api_key": CONFIG["tmdb_api_key"]}, timeout=5).json()
        return ep_res.get('name')
    except Exception as e:
        logger.error(f"TMDB Error: {e}")
        return None

def get_inferred_language(entry):
    subs = entry.get("subtitles") or {}
    auto = entry.get("automatic_captions") or {}
    if subs: return list(subs.keys())[0].split('-')[0]
    if auto: return list(auto.keys())[0].split('-')[0]
    meta_lang = entry.get("language")
    return meta_lang if (meta_lang and meta_lang != 'und') else "en"

def get_video_quality(entry):
    h = entry.get("height") or 0
    if h >= 2160: return "2160p"
    if h >= 1440: return "1440p"
    if h >= 1080: return "1080p"
    if h >= 720:  return "720p"
    return "480p"

def score_video(video, show_name, ep_title, season, ep):
    score = 0
    title = video.get("title", "").lower()
    channel = video.get("channel", "").lower()
    show_name = show_name.lower()
    
    if ep_title and ep_title.lower() in title: score += 150
    if show_name in channel: score += 80
    if show_name in title: score += 50
    try:
        ep_num = int(ep)
        if any(re.search(p, title) for p in [rf's{int(season):02d}e{ep_num:02d}', rf'ep\s*{ep_num}']):
            score += 30
    except: pass

    if any(x in title for x in ['reaction', 'review', 'trailer', 'teaser']): score -= 300
    return score

# -----------------------------------------------------------------------------
# Core Search Logic
# -----------------------------------------------------------------------------

def search_youtube(query: str, season: str = "", ep: str = "", max_results: int = 15):
    if not HAS_YTDLP: return []

    ep_title = None
    if season and ep:
        ep_title = get_episode_title_from_tmdb(query, season, ep)
    
    # We use a broader query for Stage 1 discovery
    search_str = f'"{query}" "{ep_title}"' if ep_title else f'"{query}" S{int(season or 1):02d}E{int(ep or 1):02d}'
    
    # STAGE 1: Fast Discovery
    fast_opts = {
        "quiet": True, 
        "extract_flat": "in_playlist", 
        "ignoreerrors": True  # SHIELD 1: Don't crash on one bad video
    }

    candidates = []
    try:
        with yt_dlp.YoutubeDL(fast_opts) as ydl:
            res = ydl.extract_info(f"ytsearch{max_results}:{search_str}", download=False)
            for entry in res.get("entries", []):
                if not entry: continue
                # Basic duration filter (if available in flat mode)
                if entry.get("duration") and entry.get("duration") < CONFIG["min_duration"]:
                    continue
                
                score = score_video(entry, query, ep_title, season, ep)
                if score > 0:
                    candidates.append((score, entry))
        
        # Sort by score and pick top 3 for STAGE 2 (Deep Scrape)
        top_candidates = sorted(candidates, key=lambda x: x[0], reverse=True)[:3]
        
        final_results = []
        deep_opts = {
            "quiet": True,
            "extract_flat": False,
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "ignoreerrors": True,      # SHIELD 1
            "geo_bypass": True,        # SHIELD 2: Bypass location blocks
            "no_warnings": True
        }

        with yt_dlp.YoutubeDL(deep_opts) as ydl:
            for score, fast_entry in top_candidates:
                try:
                    # Deep scrape to get quality and language
                    video_url = fast_entry.get('url') or f"https://www.youtube.com/watch?v={fast_entry['id']}"
                    info = ydl.extract_info(video_url, download=False)
                    
                    if not info: # Video is likely "Unavailable"
                        logger.warning(f"Skipping unavailable video: {fast_entry.get('id')}")
                        continue

                    final_results.append({
                        "id": info['id'],
                        "title": info['title'],
                        "url": info.get('webpage_url', video_url),
                        "channel": info.get("channel", "Unknown"),
                        "duration": info.get("duration", 0),
                        "upload_date": info.get("upload_date", ""),
                        "language": get_inferred_language(info),
                        "quality": get_video_quality(info),
                        "score": score
                    })
                except Exception as e:
                    logger.warning(f"Metadata fetch failed for {fast_entry.get('id')}: {e}")

        return final_results
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
        SubElement(item, "title").text = f"{video.get('title')} [{quality} WEBDL]"
        SubElement(item, "guid").text = hashlib.md5(video['id'].encode()).hexdigest()
        SubElement(item, "link").text = video['url']
        
        date_str = video.get("upload_date")
        dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc) if date_str else datetime.now(timezone.utc)
        SubElement(item, "pubDate").text = dt.strftime("%a, %d %b %Y %H:%M:%S %z")

        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "language", "value": video['language']})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "category", "value": "5000"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "seeders", "value": "100"})
        
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
    server = HTTPServer((CONFIG["host"], CONFIG["port"]), TorznabHandler)
    logger.info(f"Starting server on {CONFIG['port']}...")
    server.serve_forever()

if __name__ == "__main__":
    main()
