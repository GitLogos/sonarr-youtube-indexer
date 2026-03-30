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
import hashlib
import urllib.parse
import urllib.request
import json
import logging
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from xml.etree.ElementTree import Element, SubElement, tostring

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
    """Helper for Sonarr API GET requests."""
    if not CONFIG["sonarr_api_key"]:
        logger.error("SONARR_API_KEY is not set.")
        return None

    url = f"{CONFIG['sonarr_url'].rstrip('/')}/api/v3/{endpoint}"
    params = params or {}
    params["apikey"] = CONFIG["sonarr_api_key"]

    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(full_url, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        logger.error("Sonarr API error (%s): %s", endpoint, e)
        return None

def get_sonarr_metadata(series_id, season, episode):
    """Retrieves episode title and series language from Sonarr."""
    if not series_id:
        return None, "en"

    series_lang = "en"

    # 1. Try to get series metadata
    series = sonarr_api_get(f"series/{series_id}")
    if isinstance(series, dict):
        # Sonarr language fields can vary by version/setup, so keep this conservative
        original_language = series.get("originalLanguage")
        if isinstance(original_language, dict):
            name = original_language.get("name")
            if isinstance(name, str) and name:
                series_lang = name[:2].lower()

    # 2. Get episode title
    episodes = sonarr_api_get("episode", {
        "seriesId": series_id,
        "seasonNumber": season
    })

    if episodes:
        for ep in episodes:
            if str(ep.get("episodeNumber")) == str(episode):
                return ep.get("title"), series_lang

    return None, series_lang

# -----------------------------------------------------------------------------
# YouTube Metadata Helpers
# -----------------------------------------------------------------------------

def get_inferred_language(entry):
    """Infer content language from yt-dlp metadata."""
    lang = entry.get("audio_language") or entry.get("language")
    if lang and lang != "und":
        return str(lang).split("-")[0].lower()

    for key in ["subtitles", "automatic_captions"]:
        subs = entry.get(key) or {}
        if subs:
            first_key = list(subs.keys())[0]
            return first_key.replace("a.", "").split("-")[0].lower()

    return "en"

def get_video_quality(entry):
    """Infer display quality bucket from video height."""
    h = entry.get("height") or 0
    if h >= 2160:
        return "2160p"
    if h >= 1080:
        return "1080p"
    if h >= 720:
        return "720p"
    return "480p"

def score_video(video, show_name, ep_title, series_lang):
    """
    Basic scoring heuristic for fast candidate ranking.
    series_lang is currently unused directly here, but retained for future weighting.
    """
    score = 0
    title = video.get("title", "").lower()
    channel = video.get("channel", "").lower()
    show_name = (show_name or "").lower()
    ep_title_l = (ep_title or "").lower()

    # Positive matches
    if ep_title_l and ep_title_l in title:
        score += 150
    if show_name and show_name in channel:
        score += 80
    if show_name and show_name in title:
        score += 50

    # Negative signals / noise
    if any(x in title for x in ["reaction", "review", "trailer", "teaser"]):
        score -= 400

    return score

def get_video_url(entry):
    """Build a usable YouTube URL from a yt-dlp flat entry."""
    webpage_url = entry.get("webpage_url")
    if webpage_url:
        return webpage_url

    url = entry.get("url")
    if url:
        if isinstance(url, str) and (url.startswith("http://") or url.startswith("https://")):
            return url
        if isinstance(url, str):
            return f"https://www.youtube.com/watch?v={url}"

    vid = entry.get("id")
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"

    return None

def format_pubdate(upload_date):
    """
    Convert yt-dlp upload_date (YYYYMMDD) into RFC 2822 / RSS pubDate.
    Fallback to current UTC if unavailable or invalid.
    """
    if upload_date:
        try:
            dt = datetime.strptime(str(upload_date), "%Y%m%d").replace(tzinfo=timezone.utc)
            return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")
        except Exception:
            pass

    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

# -----------------------------------------------------------------------------
# Search Logic
# -----------------------------------------------------------------------------

def search_youtube(query, series_id, season, ep):
    if not HAS_YTDLP:
        logger.error("yt-dlp is not installed.")
        return []

    # 1. Fetch metadata from Sonarr
    ep_title, target_lang = get_sonarr_metadata(series_id, season, ep)

    # 2. Construct search string
    if ep_title:
        search_str = f'"{query}" "{ep_title}"'
        logger.info("Sonarr match found: episode title='%s', target_lang='%s'", ep_title, target_lang)
    else:
        try:
            season_num = int(season or 1)
        except ValueError:
            season_num = 1
        try:
            ep_num = int(ep or 1)
        except ValueError:
            ep_num = 1

        search_str = f'"{query}" S{season_num:02d}E{ep_num:02d}'
        logger.info("No Sonarr episode title found, using fallback query='%s'", search_str)

    # 3. Stage 1: Fast search
    candidates = []
    fast_opts = {
        "quiet": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
    }

    try:
        with yt_dlp.YoutubeDL(fast_opts) as ydl:
            res = ydl.extract_info(f"ytsearch10:{search_str}", download=False)
            if not res:
                logger.info("Fast search returned no response")
                return []

            entries = res.get("entries", []) or []
            logger.info("Fast search returned %d raw entries", len(entries))

            for entry in entries:
                if not entry:
                    continue

                duration = entry.get("duration")
                if duration and duration < CONFIG["min_duration"]:
                    continue

                score = score_video(entry, query, ep_title, target_lang)
                if score > 0:
                    candidates.append((score, entry))

            logger.info("Fast search retained %d scored candidates", len(candidates))

    except Exception as e:
        logger.exception("Fast YouTube search failed: %s", e)
        return []

    # 4. Stage 2: Deep scrape top candidates
    final_results = []
    deep_opts = {
        "quiet": True,
        "extract_flat": False,
        "skip_download": True,
        "ignoreerrors": True,
        "geo_bypass": True,
    }

    try:
        with yt_dlp.YoutubeDL(deep_opts) as ydl:
            # IMPORTANT FIX:
            # sort by score only so Python never tries to compare dicts on equal scores
            top_candidates = sorted(candidates, key=lambda x: x[0], reverse=True)[:3]

            for score, fast_entry in top_candidates:
                video_url = get_video_url(fast_entry)
                if not video_url:
                    continue

                info = ydl.extract_info(video_url, download=False)
                if not info:
                    continue

                vid_lang = get_inferred_language(info)
                final_score = score + (100 if vid_lang == target_lang else 0)

                final_results.append({
                    "id": info.get("id", "") or "",
                    "title": info.get("title", "Unknown Title") or "Unknown Title",
                    "url": info.get("webpage_url", video_url) or video_url,
                    "language": vid_lang,
                    "quality": get_video_quality(info),
                    "score": final_score,
                    "duration": info.get("duration", 0) or 0,
                    "upload_date": info.get("upload_date", "") or "",
                })

        # Sort final results by final score descending
        final_results = sorted(final_results, key=lambda x: x["score"], reverse=True)
        logger.info("Deep metadata stage produced %d final results", len(final_results))
        return final_results

    except Exception as e:
        logger.exception("Deep YouTube metadata lookup failed: %s", e)
        return []

# -----------------------------------------------------------------------------
# Torznab / RSS Output
# -----------------------------------------------------------------------------

def format_torznab_xml(videos):
    rss = Element("rss", {
        "version": "2.0",
        "xmlns:torznab": "http://torznab.com/schemas/2015/feed"
    })

    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = CONFIG["indexer_name"]
    SubElement(channel, "description").text = "YouTube Torznab feed"
    SubElement(channel, "link").text = "https://www.youtube.com/"
    SubElement(channel, "lastBuildDate").text = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")

    for video in videos:
        item = SubElement(channel, "item")

        q = video.get("quality", "480p")
        title = video.get("title", "Unknown Title")
        video_id = video.get("id", "")
        video_url = video.get("url", "")
        duration = video.get("duration", 0) or 0
        upload_date = video.get("upload_date", "")
        language = video.get("language", "en")

        SubElement(item, "title").text = f"{title} [{q} WEBDL]"

        guid_source = video_id if video_id else video_url
        SubElement(item, "guid").text = hashlib.md5(guid_source.encode()).hexdigest()

        SubElement(item, "link").text = video_url
        SubElement(item, "pubDate").text = format_pubdate(upload_date)
        SubElement(item, "description").text = title

        # Estimated size for Torznab clients
        duration_mins = duration / 60 if duration else 0
        size = int(duration_mins * (15 if "2160" in q else 8) * 1024 * 1024)

        SubElement(item, "size").text = str(size)
        SubElement(item, "enclosure", {
            "url": video_url,
            "length": str(size),
            "type": "application/x-bittorrent"
        })

        # Torznab attributes
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {
            "name": "language",
            "value": language
        })
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {
            "name": "category",
            "value": "5000"
        })
        SubElement(item, "{http://torznab.com/schemas/2015/feed}attr", {
            "name": "size",
            "value": str(size)
        })

    return tostring(rss, encoding="unicode")

# -----------------------------------------------------------------------------
# Web Server
# -----------------------------------------------------------------------------

class TorznabHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info("%s - %s", self.client_address[0], format % args)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        action = params.get("t", [""])[0].lower()

        logger.info("Received GET request: path=%s action=%s params=%s", self.path, action, params)

        # Optional API key validation:
        # Uncomment if you want strict Torznab-style key enforcement.
        #
        # client_api_key = params.get("apikey", [""])[0]
        # if CONFIG["api_key"] and client_api_key and client_api_key != CONFIG["api_key"]:
        #     logger.warning("Invalid API key from %s", self.client_address[0])
        #     self.send_response(403)
        #     self.send_header("Content-Type", "text/plain; charset=utf-8")
        #     self.end_headers()
        #     self.wfile.write(b"Invalid API key")
        #     return

        if action in ("search", "tvsearch"):
            q = params.get("q", [""])[0]
            sid = params.get("seriesid", [None])[0]
            s = params.get("season", ["1"])[0]
            e = params.get("ep", ["1"])[0]

            logger.info("Search request: q=%r seriesid=%r season=%r ep=%r", q, sid, s, e)

            try:
                videos = search_youtube(q, sid, s, e)
                logger.info("Returning %d videos", len(videos))
            except Exception as ex:
                logger.exception("Search failed: %s", ex)
                videos = []

            body = '<?xml version="1.0" encoding="UTF-8"?>\n' + format_torznab_xml(videos)

            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if action == "caps":
            logger.info("Capabilities request received")

            body = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<caps>'
                '<server title="YouTube" version="1.0"/>'
                '<limits max="100" default="25"/>'
                '<searching>'
                '<search available="yes" supportedParams="q"/>'
                '<tv-search available="yes" supportedParams="q,season,ep,seriesid"/>'
                '</searching>'
                '<categories>'
                '<category id="5000" name="TV"/>'
                '</categories>'
                '</caps>'
            )

            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        logger.warning("Unsupported action: %r path=%s", action, self.path)
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Unsupported action")

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    if not HAS_YTDLP:
        logger.error("yt-dlp is not installed. Please install it in the container.")
    else:
        logger.info("Starting server on %s:%s", CONFIG["host"], CONFIG["port"])
        HTTPServer((CONFIG["host"], CONFIG["port"]), TorznabHandler).serve_forever()
