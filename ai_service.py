"""
ai_service.py — AI-powered music search service for MusicBot.

Uses Google Gemini (free tier, Flash model) to convert natural-language user
prompts into targeted YouTube Music search queries, then uses yt-dlp to resolve
those queries into concrete song metadata.

Environment variable required:
    GEMINI_API_KEY — obtain for free at https://aistudio.google.com
"""

import asyncio
import json
import logging
import os
import re

import httpx
import yt_dlp

import settings
from yt_query_logic import build_yt_music_search_url, sanitize_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Gemini configuration
# ---------------------------------------------------------------------------

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def get_gemini_api_key() -> str:
    """Return the currently configured Gemini API key."""
    return os.getenv("GEMINI_API_KEY", "").strip()

GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_ENDPOINT: str = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)

_SYSTEM_PROMPT = """You are a music curator AI for a Discord music bot called MusicBỏTúi.
The users are Vietnamese and may describe their mood or music request in Vietnamese or English.

Your task: Given a user's natural-language description of mood, genre, or style, generate
{count} distinct YouTube Music search queries that will find relevant songs.

Rules:
- Each query must be concise and targeted (3–8 words), optimised for YouTube Music search.
- Vary artist styles, eras, sub-genres, and tempos to give diverse results.
- Understand Vietnamese mood words (e.g. "chill", "buồn" = sad, "sôi động" = upbeat,
  "tâm trạng" = emotional, "lo-fi buổi tối" = late-night lo-fi).
- Do NOT include playlist URLs — only search terms.
- Output ONLY a valid JSON array of strings with no extra text, markdown, or explanation.

Example output for "nhạc lo-fi chill buổi tối":
["lofi hip hop chill study beats", "late night lo-fi jazz instrumental", "chill lo-fi beats for sleep", "lo-fi city pop japanese", "ambient bedroom pop night vibes"]
"""


async def generate_search_queries(prompt: str, count: int = 8) -> list[str]:
    """Call Gemini Flash to turn a user prompt into YouTube Music search queries.

    Args:
        prompt: Natural-language description (Vietnamese or English).
        count: Number of distinct search queries to generate.

    Returns:
        List of search query strings.

    Raises:
        ValueError: If GEMINI_API_KEY is not configured or response is malformed.
        httpx.HTTPStatusError: On Gemini API error.
    """
    api_key = get_gemini_api_key()
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is not set. Add it to music-bot/.env. "
            "Obtain a free key at https://aistudio.google.com"
        )

    user_text = (
        f"{_SYSTEM_PROMPT.format(count=count)}\n\n"
        f"User request: \"{sanitize_query(prompt)}\"\n"
        f"Generate exactly {count} search queries."
    )

    payload = {
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": 0.85,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
            "candidateCount": 1,
        },
    }

    model = os.getenv("GEMINI_MODEL", GEMINI_MODEL).strip()
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0)) as client:
        resp = await client.post(
            f"{endpoint}?key={api_key}",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

    try:
        raw_text: str = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError) as exc:
        raise ValueError(f"Unexpected Gemini response structure: {data}") from exc

    try:
        queries = json.loads(raw_text)
    except Exception:
        # Extract the first JSON array in the response if wrapped in markdown or text
        match = re.search(r"\[.*?\]", raw_text, re.DOTALL)
        if not match:
            raise ValueError(f"Could not parse JSON array from Gemini response: {raw_text!r}")
        queries = json.loads(match.group())

    return [str(q).strip() for q in queries if q and str(q).strip()]


# ---------------------------------------------------------------------------
# yt-dlp search helpers
# ---------------------------------------------------------------------------

def _clean_artist_and_title(raw_title: str, raw_uploader: str) -> tuple[str, str]:
    """Extract a clean (artist, title) pair from YouTube metadata and video title."""
    title = raw_title.strip()
    artist = raw_uploader.strip()

    # Strip common channel suffixes
    if artist.endswith(" - Topic"):
        artist = artist[:-8].strip()
    elif artist.endswith("VEVO"):
        artist = artist[:-4].strip()

    # Check for "Artist - Title" pattern in the title string
    for sep in [" - ", " – ", " — "]:
        if sep in title:
            parts = title.split(sep, 1)
            candidate_artist = parts[0].strip()
            candidate_title = parts[1].strip()
            if candidate_artist and candidate_title:
                if not artist or "official" in artist.lower() or "channel" in artist.lower() or candidate_artist.lower() in artist.lower():
                    artist = candidate_artist
                    title = candidate_title
                elif not artist:
                    artist = candidate_artist
                    title = candidate_title
            break

    # Clean title noise like (Official Music Video), [Official Audio], etc.
    title = re.sub(
        r'\s*[\(\[](?:Official\s*(?:Music\s*)?(?:Video|Audio|MV|Lyric\s*Video|Visualizer)|MV|Lyrics?|HD|4K)[\)\]]',
        '',
        title,
        flags=re.IGNORECASE,
    ).strip()

    if not artist:
        artist = "Unknown Artist"

    return artist, title


def _extract_thumbnail(entry: dict) -> str:
    """Extract the best available thumbnail URL from yt-dlp entry."""
    thumb = entry.get("thumbnail") or ""
    if not thumb and entry.get("thumbnails"):
        thumbs = entry.get("thumbnails")
        if isinstance(thumbs, list) and len(thumbs) > 0:
            thumb = thumbs[-1].get("url", "")
    video_id = entry.get("id")
    if not thumb and video_id:
        thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return str(thumb).strip()


def _extract_url(entry: dict) -> str:
    """Ensure a full, canonical YouTube watch URL."""
    url = (entry.get("webpage_url") or entry.get("url") or "").strip()
    video_id = entry.get("id")
    if (not url or not url.startswith("http")) and video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"
    return url


def _ytdl_flat_search(search_target: str, max_results: int) -> list[dict]:
    """Blocking call — run inside executor. Extracts flat metadata for a search query or URL."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
        "playlistend": max_results,
        "ignoreerrors": True,
        "default_search": "auto",
        "source_address": "0.0.0.0",
    }
    # Reuse extractor_args from bot config (e.g. android/ios player_client)
    try:
        base = settings.get_search_ytdl_options()
        opts["extractor_args"] = base.get("extractor_args", {})
    except Exception:
        pass

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(search_target, download=False)
    if not info:
        return []
    entries = info.get("entries") or ([info] if info.get("id") else [])
    return [e for e in entries if e][:max_results]


async def search_songs(
    queries: list[str],
    max_per_query: int = 2,
    blacklist_patterns: list[str] | None = None,
    recent_titles: set[str] | None = None,
) -> list[dict]:
    """Search YouTube for each query and return deduplicated, filtered results with full metadata.

    Args:
        queries: List of search query strings (from Gemini or direct input).
        max_per_query: Maximum entries to pull from each individual query.
        blacklist_patterns: List of regex patterns to filter out (from config.yaml).
        recent_titles: Set of lowercased song titles already in queue (for dedup).

    Returns:
        List of song dicts: {title, artist, duration, url, thumbnail}.
    """
    blacklist_patterns = blacklist_patterns or []
    recent_titles = recent_titles or set()

    results: list[dict] = []
    seen_titles: set[str] = set(recent_titles)  # copy to avoid mutating caller's set
    loop = asyncio.get_event_loop()
    semaphore = asyncio.Semaphore(4)

    async def _search_one(query: str) -> list[dict]:
        search_target = f"ytsearch{max_per_query}:{sanitize_query(query)}"
        async with semaphore:
            try:
                return await loop.run_in_executor(
                    None, _ytdl_flat_search, search_target, max_per_query
                )
            except Exception as exc:
                logger.warning("[AI] yt-dlp search failed for %r: %s", query, exc)
                return []

    # Run searches concurrently with controlled parallelism
    query_results = await asyncio.gather(*(_search_one(q) for q in queries))

    for entries in query_results:
        for entry in entries:
            if not entry:
                continue
            raw_title: str = (entry.get("title") or "").strip()
            if not raw_title:
                continue

            raw_uploader: str = (
                entry.get("uploader")
                or entry.get("channel")
                or entry.get("uploader_id")
                or ""
            ).strip()

            artist, title = _clean_artist_and_title(raw_title, raw_uploader)
            title_key = title.lower()

            # --- Deduplication ---
            if title_key in seen_titles:
                continue

            # --- Blacklist filter ---
            if any(
                re.search(pattern, title, re.IGNORECASE)
                or re.search(pattern, raw_title, re.IGNORECASE)
                for pattern in blacklist_patterns
            ):
                logger.debug("[AI] Skipped blacklisted: %r", title)
                continue

            url = _extract_url(entry)
            if not url:
                continue

            seen_titles.add(title_key)
            results.append(
                {
                    "title": title,
                    "artist": artist,
                    "duration": entry.get("duration") or 0,
                    "url": url,
                    "thumbnail": _extract_thumbnail(entry),
                }
            )

    logger.info("[AI] search_songs: %d queries → %d unique results", len(queries), len(results))
    return results
