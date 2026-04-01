#!/usr/bin/env python3
"""
YouTube Indexer for Sonarr (v8.0 - RFC 822 Date Fix)
=====================================================
Fixes:
- Added RFC 822 compliant pubDate for Prowlarr compatibility.
- Improved dummy result for connection tests.
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
# Helpers
# -----------------------------------------------------------------------------

def format_rfc822_date(date_str):
    """Converts YYYYMMDD or ISO time strings to RFC 822 date string."""
    dt = None
    if isinstance(date_str, str):
        date_str = date_str.strip()
        if re.fullmatch(r"\d{8}", date_str):
            try:
                dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
            except ValueError:
                dt = None
        else:
            try:
                dt = datetime.fromisoformat(date_str)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
            except Exception:
                dt = None
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z")


def sonarr_api_get(endpoint, params=None):
    if not CONFIG["sonarr_api_key"]: return None
    url = f"{CONFIG['sonarr_url'].rstrip('/')}/api/v3/{endpoint}"
    params = params or {}
    params["apikey"] = CONFIG["sonarr_api_key"]
    try:
        full_url = f"{url}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(full_url, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        logger.error(f"Sonarr API error: {e}")
        return None

def get_sonarr_metadata(series_id, season, episode):
    if not series_id:
        return None, "en"

    try:
        series_id = int(series_id)
    except (TypeError, ValueError):
        return None, "en"

    series = sonarr_api_get(f"series/{series_id}")
    series_lang = "en"
    if series:
        lang_data = series.get('languageProfile') or series.get('language')
        if isinstance(lang_data, dict):
            series_lang = lang_data.get('name', 'en').lower()[:2]
        elif isinstance(lang_data, str):
            series_lang = lang_data.lower()[:2]

    episodes = sonarr_api_get("episode", {"seriesId": series_id})
    if episodes:
        for ep_data in episodes:
            if str(ep_data.get('seasonNumber')) == str(season) and str(ep_data.get('episodeNumber')) == str(episode):
                return ep_data.get('title'), series_lang

    return None, series_lang

def get_inferred_language(entry):
    preferred = entry.get("audio_language") or entry.get("language")
    if preferred and preferred.lower() != 'und':
        return preferred.split('-')[0].lower()

    for key in ["automatic_captions", "subtitles"]:
        caps = entry.get(key) or {}
        if isinstance(caps, dict) and caps:
            for raw_key in caps.keys():
                if not raw_key:
                    continue
                lang_code = raw_key.replace('a.', '').split('-')[0].lower()
                if lang_code and lang_code != 'und':
                    return lang_code

    # last resort
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
    if any(x in title for x in ['reaction', 'review', 'trailer', 'teaser', 'clips']):
        score -= 400
    return score

# -----------------------------------------------------------------------------
# Logic
# -----------------------------------------------------------------------------

def search_youtube(query, series_id, season, ep):
    if not query:
        return []

    query = query.strip()
    try:
        season_int = int(season) if season is not None else 1
    except (TypeError, ValueError):
        season_int = 1
    try:
        ep_int = int(ep) if ep is not None else 1
    except (TypeError, ValueError):
        ep_int = 1

    ep_title, target_lang = get_sonarr_metadata(series_id, season_int, ep_int)
    search_str = f'"{query}" "{ep_title}"' if ep_title else f'"{query}" S{season_int:02d}E{ep_int:02d}'
    logger.info(f"Executing Search: {search_str} (Lang: {target_lang})")

    candidates = []
    fast_opts = {
        "quiet": True,
        "ignoreerrors": True,
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }

    with yt_dlp.YoutubeDL(fast_opts) as ydl:
        try:
            res = ydl.extract_info(f"ytsearch10:{search_str}", download=False)
        except Exception as e:
            logger.error(f"yt-dlp initial search failed: {e}")
            return []

    if not res or not isinstance(res, dict):
        return []

    for entry in res.get("entries", []):
        if not entry:
            continue
        duration = entry.get("duration") or 0
        if duration < CONFIG["min_duration"]:
            continue
        score = score_video(entry, query, ep_title)
        if score > 0:
            candidates.append((score, entry))

    final_results = []
    top_candidates = sorted(candidates, key=lambda x: x[0], reverse=True)[:5]
    deep_opts = {
        "quiet": True,
        "ignoreerrors": True,
        "skip_download": True,
        "noplaylist": True,
        "geo_bypass": True,
        "source_address": "0.0.0.0",
    }

    with yt_dlp.YoutubeDL(deep_opts) as ydl:
        for score, fast_entry in top_candidates:
            try:
                video_id = fast_entry.get('id') or fast_entry.get('url')
                if not video_id:
                    continue
                v_url = fast_entry.get('url') or f"https://www.youtube.com/watch?v={video_id}"
                info = ydl.extract_info(v_url, download=False)
                if not info:
                    continue

                vid_lang = get_inferred_language(info)
                final_results.append({
                    "id": info.get('id', video_id),
                    "title": info.get('title', 'Unknown Title'),
                    "url": info.get('webpage_url', v_url),
                    "language": vid_lang,
                    "quality": get_video_quality(info),
                    "score": score + (100 if vid_lang == target_lang else 0),
                    "duration": info.get('duration', 0),
                    "upload_date": info.get('upload_date', '')
                })
            except Exception as e:
                logger.warning(f"yt-dlp candidate detail fetch failed for {fast_entry.get('id')}: {e}")

    return sorted(final_results, key=lambda x: x['score'], reverse=True)

# -----------------------------------------------------------------------------
# XML & Server
# -----------------------------------------------------------------------------

def format_torznab_xml(videos):
    rss = Element("rss", {"version": "2.0", "xmlns:torznab": "http://torznab.com/schemas/2015/feed"})
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = CONFIG["indexer_name"]

    for video in videos:
        item = SubElement(channel, "item")
        q = video.get('quality', '480p')
        title = video.get('title', 'Unknown')
        vid_id = video.get('id', '')

        SubElement(item, "title").text = f"{title} [{q} WEBDL]"
        guid_value = f"{vid_id}-{q}-{video.get('upload_date','')}-{video.get('language','') }"
        SubElement(item, "guid").text = hashlib.md5(guid_value.encode('utf-8')).hexdigest()
        SubElement(item, "link").text = video.get('url', '')
        SubElement(item, "pubDate").text = format_rfc822_date(video.get('upload_date'))

        duration_secs = video.get('duration') or 0
        duration_mins = max(duration_secs / 60.0, 1)
        size_bytes = int(duration_mins * (15 if "2160" in q else 8) * 1024 * 1024)

        SubElement(item, "size").text = str(size_bytes)
        SubElement(item, "enclosure", {"url": video.get('url', ''), "length": str(size_bytes), "type": "application/x-bittorrent"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "language", "value": video.get('language', 'en')})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "category", "value": "5000"})
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {"name": "seeders", "value": "100"})

    return tostring(rss, encoding="unicode")

class TorznabHandler(BaseHTTPRequestHandler):
    def _send_xml(self, xml_content):
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(xml_content.encode("utf-8"))

    def do_GET(self):
        logger.info(f"GET Request: {self.path}")
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        # API key authentication is disabled to allow open access during troubleshooting.
        action = params.get("t", [""])[0].lower()

        if action in ("search", "tvsearch"):
            q = params.get("q", [""])[0]
            if not q or q.strip() == "":
                logger.info("Empty query (Prowlarr test). Returning valid dummy RSS.")
                dummy = [{
                    "id": "prowlarr_test", "title": "YouTube Indexer Connection Test",
                    "url": "https://youtube.com", "language": "en", "quality": "1080p",
                    "score": 100, "duration": 600, "upload_date": "20260101"
                }]
                self._send_xml('<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(dummy))
                return

            series_id = params.get("seriesid", [None])[0]
            season = params.get("season", ["1"])[0]
            ep = params.get("ep", ["1"])[0]
            videos = search_youtube(q, series_id, season, ep)
            self._send_xml('<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(videos))

        elif action == "caps":
            self._send_xml('<?xml version="1.0" encoding="UTF-8"?>\n'
                           + '<caps><server title="YouTube" version="1.0"/><searching>'
                           + '<search available="yes" supportedParams="q"/>'
                           + '<tv-search available="yes" supportedParams="q,season,ep,seriesid"/>'
                           + '</searching></caps>')

        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Invalid request: missing or bad 't' parameter")

if __name__ == "__main__":
    if not HAS_YTDLP: print("yt-dlp missing.")
    else: HTTPServer((CONFIG["host"], CONFIG["port"]), TorznabHandler).serve_forever()
