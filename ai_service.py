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

_SYSTEM_PROMPT = """You are an expert music curator AI for a Discord music bot called MusicBỏTúi.
The users may describe their desired vibe, mood, genre, or style in Vietnamese or English.

Your task: Given a user's prompt, generate {count} distinct, high-quality search queries that identify specific, famous, popular hit songs that fit the vibe.

Strict Rules:
1. Specific Songs: Target specific, real songs formatted as "Artist Name - Song Title" (e.g. "Sơn Tùng M-TP - Lạc Trôi", "Vũ. - Bước Qua Nhau", "Đen - Trốn Tìm", "The Weeknd - Blinding Lights").
2. No Duplicates: Every query MUST be a DIFFERENT song by a DIFFERENT artist. NEVER suggest multiple songs by the same artist in the same batch, and never suggest different versions of the same song.
3. Original Studio Releases: NEVER query for live versions, fan-cams, covers, karaoke, lyrics videos, 1-hour loops, speed-up/slowed versions, or mashups unless the user specifically requested them in their prompt. Focus on official original releases.
4. No Generic Compilation Terms: Do NOT output generic terms like "top hits playlist", "nhạc chill hay nhất", "lofi beats compilation", or "mashup". Target concrete tracks.
5. Vietnamese Context: Seamlessly understand Vietnamese mood terms (e.g. "chill", "buồn", "suy", "tâm trạng", "sôi động", "quẩy", "nhạc trẻ", "bolero", "rap việt", "indie việt", "nhạc US-UK").
6. Format: Output ONLY a valid JSON array of {count} strings. No markdown formatting, no explanations.

Example output for "nhạc vpop chill hoàng hôn":
["Vũ. - Bước Qua Mùa Cô Đơn", "Chillies - Có Em Đời Bỗng Vui", "Hoàng Dũng - Nàng Thơ", "Thịnh Suy - Một Đêm Say", "Trang - Thư Cho Anh"]
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


def normalize_track_fingerprint(artist: str, title: str) -> tuple[str, str]:
    """Generate a canonical dedup fingerprint (norm_artist, norm_title).
    
    Strips noise words, version modifiers (Live, Remix, Lyrics, Vietsub, Acoustic, Cover,
    Karaoke, HD, 4K, Official, MV, etc.), featuring artists, and punctuation.
    """
    def _clean(s: str) -> str:
        # Strip brackets containing metadata/version keywords
        s = re.sub(
            r'[\(\[][^\)\]]*(?:official|live|lyric|lyrics|mv|audio|remix|acoustic|ver|version|'
            r'karaoke|vietsub|engsub|stage|show|cover|concert|tour|fancam|performance|gala|'
            r'mashup|tuyển\s*tập|bản\s*nhạc|album|full|visualizer|hd|4k)[^\)\]]*[\)\]]',
            '', s, flags=re.IGNORECASE,
        )
        # Strip inline words like 'hát Live', 'Live at ...', 'Acoustic Ver'
        s = re.sub(r'\b(?:hát\s+)?live(?:\s+acoustic)?(?:\s+ver(?:sion)?)?\.?\b', '', s, flags=re.IGNORECASE)
        s = re.sub(r'\b(?:remix|lyrics?|karaoke|vietsub|engsub|acoustic|cover|visualizer)\b', '', s, flags=re.IGNORECASE)
        s = re.sub(r'\s*(?:ft\.?|feat\.?|featuring)\s+.*$', '', s, flags=re.IGNORECASE)
        s = re.sub(r'\s*\|\s*.*$', '', s)
        s = re.sub(r'#\S+', '', s)
        return re.sub(r'[^a-zA-Z0-9\u00C0-\u1EF9]+', '', s.lower()).strip()

    c_art = _clean(artist)
    c_tit = title
    if artist:
        c_tit = re.sub(re.escape(artist), '', c_tit, flags=re.IGNORECASE)
    t_clean = _clean(c_tit) or _clean(title)
    return c_art, t_clean


def _score_candidate(entry: dict, prompt: str = "") -> float:
    """Score candidate videos based on view counts and official signals, filtering low views and noise."""
    views = entry.get("view_count")
    duration = entry.get("duration") or 0
    raw_title = (entry.get("title") or "").strip()
    raw_uploader = (entry.get("uploader") or entry.get("channel") or "").strip()

    # Drop videos that are too short (< 45s) or too long (> 15m, playlists/compilations)
    if duration and (duration < 45 or duration > 900):
        return -1.0

    # Avoid videos with almost to no views (YouTube algorithm often pushes 0-view unknown videos)
    if views is not None and views < 1000:
        return -1.0

    base_score = float(views if views is not None else 1000)

    title_lower = raw_title.lower()
    uploader_lower = raw_uploader.lower()
    prompt_lower = (prompt or "").lower()

    user_wants_live = "live" in prompt_lower
    user_wants_remix = "remix" in prompt_lower
    user_wants_acoustic = "acoustic" in prompt_lower

    # Penalize secondary versions (live, remix, lyrics, karaoke, cover)
    penalties = [
        ("live", 0.1, user_wants_live),
        ("lyrics", 0.15, False),
        ("lyric", 0.15, False),
        ("vietsub", 0.15, False),
        ("engsub", 0.15, False),
        ("karaoke", 0.05, False),
        ("remix", 0.2, user_wants_remix),
        ("acoustic", 0.25, user_wants_acoustic),
        ("cover", 0.1, False),
        ("fancam", 0.05, False),
        ("reaction", 0.01, False),
        ("1 hour", 0.01, False),
        ("10 hours", 0.01, False),
        ("speed up", 0.05, False),
        ("slowed", 0.05, False),
        ("instrumental", 0.1, False),
        ("beat", 0.1, False),
    ]

    for term, factor, allowed in penalties:
        if not allowed and term in title_lower:
            base_score *= factor

    # Boost official artist channels and official releases
    if "official" in title_lower or "official" in uploader_lower or raw_uploader.endswith(" - Topic"):
        base_score *= 2.0

    return base_score


def _is_duplicate(
    norm_art: str,
    norm_tit: str,
    seen_fingerprints: set[str],
    seen_canonical: set[str],
) -> bool:
    """Check whether a track is a duplicate of an existing song, live version, lyric video, or remix."""
    composite = f"{norm_art}:{norm_tit}" if norm_art else norm_tit
    combined = f"{norm_art}{norm_tit}" if norm_art else norm_tit

    if composite in seen_fingerprints:
        return True
    if len(norm_tit) >= 4 and norm_tit in seen_canonical:
        return True
    if len(combined) >= 4 and combined in seen_canonical:
        return True

    # Substring check for embedded artist + title (e.g. "sơntùngmtplạctrôi" vs "lạctrôi")
    for seen in seen_canonical:
        if len(seen) >= 6 and (seen in norm_tit or seen in combined):
            return True
        if len(norm_tit) >= 6 and (norm_tit in seen):
            return True

    return False


async def search_songs(
    queries: list[str],
    max_per_query: int = 2,
    blacklist_patterns: list[str] | None = None,
    recent_titles: set[str] | None = None,
    prompt: str = "",
) -> list[dict]:
    """Search YouTube for each query and return deduplicated, filtered results with full metadata.

    Args:
        queries: List of search query strings (from Gemini or direct input).
        max_per_query: Maximum entries to pull from each individual query.
        blacklist_patterns: List of regex patterns to filter out (from config.yaml).
        recent_titles: Set of song titles already in queue or playing (for dedup).
        prompt: Optional user prompt to preserve intentionally requested vibes (e.g. remix, live).

    Returns:
        List of song dicts: {title, artist, duration, url, thumbnail}.
    """
    blacklist_patterns = blacklist_patterns or []
    recent_titles = recent_titles or set()

    results: list[dict] = []
    seen_raw_titles: set[str] = set()
    seen_fingerprints: set[str] = set()
    seen_titles_canonical: set[str] = set()

    # Pre-populate dedup sets from recent queue titles
    for rec in recent_titles:
        rec_clean = rec.strip()
        if rec_clean:
            seen_raw_titles.add(rec_clean.lower())
            rec_art, rec_tit = normalize_track_fingerprint("", rec_clean)
            if rec_tit:
                seen_titles_canonical.add(rec_tit)
            if rec_art and rec_tit:
                seen_fingerprints.add(f"{rec_art}:{rec_tit}")
                seen_titles_canonical.add(f"{rec_art}{rec_tit}")

    loop = asyncio.get_event_loop()
    semaphore = asyncio.Semaphore(4)
    fetch_candidates_per_query = max(max_per_query * 3, 5)

    async def _search_one(query: str) -> list[dict]:
        search_target = f"ytsearch{fetch_candidates_per_query}:{sanitize_query(query)}"
        async with semaphore:
            try:
                entries = await loop.run_in_executor(
                    None, _ytdl_flat_search, search_target, fetch_candidates_per_query
                )
                # Score and sort candidates within this query by view counts & authenticity
                scored = []
                for e in entries:
                    if not e:
                        continue
                    sc = _score_candidate(e, prompt=prompt)
                    if sc > 0:
                        scored.append((sc, e))
                scored.sort(key=lambda x: x[0], reverse=True)
                return [e for _, e in scored]
            except Exception as exc:
                logger.warning("[AI] yt-dlp search failed for %r: %s", query, exc)
                return []

    # Run searches concurrently with controlled parallelism
    query_results = await asyncio.gather(*(_search_one(q) for q in queries))

    for entries in query_results:
        added_for_query = 0
        for entry in entries:
            if added_for_query >= max_per_query:
                break
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
            title_lower = title.lower()

            # --- Deduplication by raw title ---
            if title_lower in seen_raw_titles:
                continue

            # --- Canonical Fingerprint Deduplication (eliminates live, lyrics, remix duplicates) ---
            norm_art, norm_tit = normalize_track_fingerprint(artist, title)
            composite_key = f"{norm_art}:{norm_tit}" if norm_art else norm_tit

            if _is_duplicate(norm_art, norm_tit, seen_fingerprints, seen_titles_canonical):
                logger.debug("[AI] Skipped duplicate track %r (artist: %r)", title, artist)
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

            seen_raw_titles.add(title_lower)
            seen_fingerprints.add(composite_key)
            if len(norm_tit) >= 4:
                seen_titles_canonical.add(norm_tit)
            if norm_art and len(norm_tit) >= 3:
                seen_titles_canonical.add(f"{norm_art}{norm_tit}")

            results.append(
                {
                    "title": title,
                    "artist": artist,
                    "duration": entry.get("duration") or 0,
                    "url": url,
                    "thumbnail": _extract_thumbnail(entry),
                }
            )
            added_for_query += 1

    logger.info("[AI] search_songs: %d queries → %d unique high-quality results", len(queries), len(results))
    return results
