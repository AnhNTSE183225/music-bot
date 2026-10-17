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

from pathlib import Path
import db

try:
    from dotenv import load_dotenv
    _env_file = Path(__file__).resolve().parent / ".env"
    if _env_file.exists():
        load_dotenv(dotenv_path=_env_file)
    load_dotenv()
except ImportError:
    pass

DEPRECATED_MODELS = {"gemini-1.5-flash", "gemini-1.5-pro", "gemini-1.0-pro"}

def get_gemini_model() -> str:
    """Return configured model, falling back to gemini-3.5-flash-lite if deprecated or missing."""
    configured = getattr(settings, "AI_MODEL", None) or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    configured = str(configured).strip()
    if configured in DEPRECATED_MODELS or not configured:
        return "gemini-3.5-flash-lite"
    return configured

GEMINI_MODEL: str = get_gemini_model()

def _parse_retry_delay(data: dict, fallback_seconds: float = 21600.0) -> float:
    """Extract exact retry delay from Google Gemini 429 response body."""
    # 1. Inspect details for RetryInfo (e.g. {"@type": "...RetryInfo", "retryDelay": "22507s"})
    details = data.get("error", {}).get("details", [])
    if isinstance(details, list):
        for item in details:
            if isinstance(item, dict) and "retryDelay" in item:
                val = str(item["retryDelay"]).strip()
                if val.endswith("s"):
                    try:
                        return float(val[:-1])
                    except ValueError:
                        pass

    # 2. Inspect error message regex like "retry in 6h15m7s"
    msg = data.get("error", {}).get("message", "")
    m = re.search(r'retry in\s+(?:(\d+)h)?(?:(\d+)m)?(?:([\d\.]+)s)?', msg, re.IGNORECASE)
    if m:
        hours = float(m.group(1) or 0)
        minutes = float(m.group(2) or 0)
        seconds = float(m.group(3) or 0)
        total = hours * 3600 + minutes * 60 + seconds
        if total > 0:
            return total

    return fallback_seconds


async def verify_gemini_api_key(api_key: str) -> dict:
    """Probe Google Gemini endpoint to verify if an API key is valid, exhausted, or rejected.

    Returns:
        dict: {
            "status": "healthy" | "quota_exceeded" | "invalid" | "error",
            "message": str,
            "retry_delay": float
        }
    """
    key = api_key.strip().strip('"').strip("'")
    if not key:
        return {"status": "error", "message": "API key cannot be empty.", "retry_delay": 0.0}

    model = get_gemini_model()
    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [{"parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 5},
    }

    last_exc = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=15.0)) as client:
                resp = await client.post(endpoint, json=payload, headers={"Content-Type": "application/json"})
                if resp.status_code == 200:
                    return {"status": "healthy", "message": "Key is healthy and operational.", "retry_delay": 0.0}

                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                err_msg = data.get("error", {}).get("message", resp.text)

                if resp.status_code == 429:
                    delay = _parse_retry_delay(data)
                    return {
                        "status": "quota_exceeded",
                        "message": f"Quota exceeded ({err_msg})",
                        "retry_delay": delay,
                    }
                elif resp.status_code in (400, 403):
                    return {
                        "status": "invalid",
                        "message": f"Google rejected key: {err_msg}",
                        "retry_delay": 0.0,
                    }
                elif resp.status_code == 404:
                    return {
                        "status": "invalid",
                        "message": f"Model not found: {err_msg}",
                        "retry_delay": 0.0,
                    }
                else:
                    return {
                        "status": "invalid",
                        "message": f"HTTP {resp.status_code}: {err_msg}",
                        "retry_delay": 0.0,
                    }
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue

    exc_detail = str(last_exc).strip() or repr(last_exc) or type(last_exc).__name__
    logger.warning(f"[AI Key Probe] Network error probing Gemini key: {exc_detail}")
    return {
        "status": "error",
        "message": f"Connection to Google API failed ({type(last_exc).__name__}): {exc_detail}",
        "retry_delay": 0.0,
    }


_SYSTEM_PROMPT = """You are an expert music curator AI for a Discord music bot called MusicBỏTúi.
The users may describe their desired vibe, mood, genre, or style in Vietnamese or English.

Your task: Given a user's prompt, generate {count} distinct, high-quality search queries that identify specific, famous, popular hit songs that fit the vibe.

Strict Rules:
1. Specific Songs: Target specific, real songs formatted as "Artist Name - Song Title" (e.g. "Sơn Tùng M-TP - Lạc Trôi", "Vũ. - Bước Qua Nhau", "Đen - Trốn Tìm", "The Weeknd - Blinding Lights").
2. Artist Focus vs. General Vibes:
   - If the user specifically asks for a particular singer, band, or artist (e.g., "Phùng Khánh Linh", "nhạc Sơn Tùng", "Taylor Swift songs", "Imagine Dragons"): ALL or the vast majority of generated songs MUST be distinct songs by THAT requested artist! Do not substitute other artists unless the requested artist has fewer songs than requested.
   - If the user describes a general mood, genre, or vibe without a specific artist (e.g., "nhạc chill hoàng hôn", "lo-fi study", "vietnamese indie"): provide variety with different artists (avoid repeating the same artist multiple times).
3. Original Studio Releases: NEVER query for live versions, fan-cams, covers, karaoke, lyrics videos, 1-hour loops, speed-up/slowed versions, or mashups unless the user specifically requested them in their prompt. Focus on official original releases.
4. No Generic Compilation Terms: Do NOT output generic terms like "top hits playlist", "nhạc chill hay nhất", "lofi beats compilation", or "mashup". Target concrete tracks.
5. Vietnamese Context: Seamlessly understand Vietnamese mood and artist terms (e.g. "nhạc của [ca sĩ]", "chill", "buồn", "suy", "tâm trạng", "sôi động", "quẩy", "nhạc trẻ", "bolero", "rap việt", "indie việt", "nhạc US-UK").
6. Format: Output ONLY a valid JSON array of {count} strings. No markdown formatting, no explanations.

Example output for "nhạc vpop chill hoàng hôn":
["Vũ. - Bước Qua Mùa Cô Đơn", "Chillies - Có Em Đời Bỗng Vui", "Hoàng Dũng - Nàng Thơ", "Thịnh Suy - Một Đêm Say", "Trang - Thư Cho Anh"]

Example output for "nhạc Phùng Khánh Linh":
["Phùng Khánh Linh - Hôm Nay Tôi Buồn", "Phùng Khánh Linh - Căn Gác Mùa Thu", "Phùng Khánh Linh - Cô Gái Nhân Ái", "Phùng Khánh Linh - Quý Cô Say Xỉn", "Phùng Khánh Linh - Chỉ Còn Lại Hai Ta"]
"""


async def generate_search_queries(prompt: str, count: int = 8) -> list[str]:
    """Call Gemini Flash to turn a user prompt into YouTube Music search queries using rotating keys.

    Args:
        prompt: Natural-language description (Vietnamese or English).
        count: Number of distinct search queries to generate.

    Returns:
        List of search query strings.

    Raises:
        ValueError: If no active healthy keys remain or all hit quota.
    """
    available_keys = db.get_available_gemini_keys()

    if not available_keys:
        all_keys = db.get_all_gemini_keys_for_display()
        if not all_keys:
            raise ValueError(
                "No Gemini API keys in pool. Please contribute an API key in the UI."
            )
        # Find minimum remaining cooldown
        cooldowns = [k["cooldown_remaining_seconds"] for k in all_keys if k["cooldown_remaining_seconds"] > 0]
        min_wait = min(cooldowns) if cooldowns else 3600
        mins = max(1, int(min_wait // 60))
        raise ValueError(
            f"All {len(all_keys)} Gemini API keys have exceeded their quota. "
            f"Next key resets in ~{mins} minute(s). You can contribute an active key in the UI to keep it running immediately!"
        )

    user_text = (
        f"{_SYSTEM_PROMPT.format(count=count)}\n\n"
        f"User request: \"{sanitize_query(prompt)}\"\n"
        f"Generate exactly {count} search queries."
    )

    temperature = float(getattr(settings, "AI_TEMPERATURE", 0.85))
    max_output_tokens = int(getattr(settings, "AI_MAX_OUTPUT_TOKENS", 2048))
    fallback_cooldown_sec = float(getattr(settings, "AI_COOLDOWN_HOURS", 6.0)) * 3600.0

    payload = {
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "candidateCount": 1,
        },
    }

    model = get_gemini_model()
    last_error = ""

    # Rotate through healthy keys with immediate automatic failover
    for key_entry in available_keys:
        key_id = key_entry["id"]
        api_key = key_entry["api_key"]
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0)) as client:
                resp = await client.post(
                    f"{endpoint}?key={api_key}",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

                if resp.status_code == 404 and model != "gemini-3.8-flash":
                    logger.warning("[AI] Model %r returned 404. Falling back to 'gemini-3.8-flash'...", model)
                    model = "gemini-3.8-flash"
                    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                    resp = await client.post(
                        f"{endpoint}?key={api_key}",
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )

                if resp.status_code == 429:
                    # Parse dynamic retryDelay from Google
                    try:
                        err_json = resp.json()
                    except Exception:
                        err_json = {}
                    retry_sec = _parse_retry_delay(err_json, fallback_seconds=fallback_cooldown_sec)
                    db.mark_gemini_key_quota_exceeded(key_id, cooldown_seconds=retry_sec, message="Google 429 quota exhausted")
                    last_error = f"Key #{key_id} quota exceeded (cooldown: {int(retry_sec // 60)}m). Trying next key..."
                    logger.warning("[AI] %s", last_error)
                    continue

                if resp.status_code in (400, 403):
                    try:
                        err_json = resp.json()
                        err_msg = err_json.get("error", {}).get("message", "")
                    except Exception:
                        err_msg = resp.text
                    db.mark_gemini_key_invalid(key_id, message=err_msg)
                    last_error = f"Key #{key_id} invalid ({err_msg}). Trying next key..."
                    logger.error("[AI] %s", last_error)
                    continue

                resp.raise_for_status()
                data = resp.json()

                raw_text: str = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                queries = []
                # 1. Direct JSON parse
                try:
                    parsed = json.loads(raw_text)
                    if isinstance(parsed, list):
                        queries = parsed
                except Exception:
                    pass

                # 2. Complete [ ... ] array match
                if not queries:
                    match = re.search(r"\[.*?\]", raw_text, re.DOTALL)
                    if match:
                        try:
                            parsed = json.loads(match.group())
                            if isinstance(parsed, list):
                                queries = parsed
                        except Exception:
                            pass

                # 3. Truncated array recovery: close trailing comma or bracket
                if not queries:
                    trimmed = raw_text.strip()
                    if trimmed.startswith("[") and not trimmed.endswith("]"):
                        # Remove trailing comma if present
                        trimmed_content = re.sub(r",\s*$", "", trimmed)
                        try:
                            parsed = json.loads(trimmed_content + "]")
                            if isinstance(parsed, list):
                                queries = parsed
                        except Exception:
                            pass

                # 4. Regex extraction of all quoted string items if json decoding fails
                if not queries:
                    found_items = re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', raw_text)
                    if found_items:
                        queries = found_items

                if not queries:
                    raise ValueError(f"Could not parse JSON array from Gemini response: {raw_text!r}")

                # Mark key usage
                db.record_gemini_key_usage(key_id)
                return [str(q).strip() for q in queries if q and str(q).strip()]

        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout) as conn_err:
            logger.warning("[AI] Network timeout with key #%d: %s", key_id, conn_err)
            last_error = f"Connection timeout: {conn_err}"
            continue
        except Exception as exc:
            logger.error("[AI] Unexpected error with key #%d: %s", key_id, exc)
            last_error = str(exc)
            continue

    raise ValueError(f"All available Gemini keys failed. Last error: {last_error}")



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
        thumb = f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
    thumb = str(thumb).strip()
    if "/hqdefault.jpg" in thumb:
        thumb = thumb.replace("/hqdefault.jpg", "/maxresdefault.jpg")
    return thumb


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

    min_views = int(getattr(settings, "AI_MIN_VIEW_COUNT", 1000))
    # Avoid videos with almost to no views (YouTube algorithm often pushes 0-view unknown videos)
    if views is not None and views < min_views:
        return -1.0

    base_score = float(views if views is not None else min_views)

    title_lower = raw_title.lower()
    uploader_lower = raw_uploader.lower()
    prompt_lower = (prompt or "").lower()

    user_wants_live = "live" in prompt_lower
    user_wants_remix = "remix" in prompt_lower
    user_wants_acoustic = "acoustic" in prompt_lower

    cfg_penalties = getattr(settings, "AI_SCORE_PENALTIES", {}) or {}

    # Penalize secondary versions (live, remix, lyrics, karaoke, cover)
    penalties = [
        ("live", float(cfg_penalties.get("live", 0.1)), user_wants_live),
        ("lyrics", float(cfg_penalties.get("lyrics", 0.15)), False),
        ("lyric", float(cfg_penalties.get("lyric", 0.15)), False),
        ("vietsub", float(cfg_penalties.get("vietsub", 0.15)), False),
        ("engsub", float(cfg_penalties.get("engsub", 0.15)), False),
        ("karaoke", float(cfg_penalties.get("karaoke", 0.05)), False),
        ("remix", float(cfg_penalties.get("remix", 0.2)), user_wants_remix),
        ("acoustic", float(cfg_penalties.get("acoustic", 0.25)), user_wants_acoustic),
        ("cover", float(cfg_penalties.get("cover", 0.1)), False),
        ("fancam", 0.05, False),
        ("reaction", 0.01, False),
        ("1 hour", 0.01, False),
        ("10 hours", 0.01, False),
        ("speed up", float(cfg_penalties.get("speed_up", 0.05)), False),
        ("slowed", float(cfg_penalties.get("slowed", 0.05)), False),
        ("instrumental", float(cfg_penalties.get("instrumental", 0.1)), False),
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
        clean_q = sanitize_query(query)
        async with semaphore:
            # 1. Try YouTube Music 'Songs' tab (#songs) first to get clean studio audio
            ytm_url = build_yt_music_search_url(clean_q)
            try:
                raw_ytm = await loop.run_in_executor(
                    None, _ytdl_flat_search, ytm_url, fetch_candidates_per_query
                )
                ytm_entries = []
                query_expected_title = query.split(" - ", 1)[1].strip().lower() if " - " in query else clean_q.lower()
                query_expected_artist = query.split(" - ", 1)[0].strip() if " - " in query else ""

                for e in raw_ytm:
                    if not e:
                        continue
                    u = e.get("url") or e.get("webpage_url") or ""
                    if "/browse/" in u:
                        continue
                    if "watch?v=" in u or "youtu.be/" in u or e.get("id"):
                        # Attach query context for artist fallback if missing from flat metadata
                        if not e.get("uploader") and not e.get("artist") and query_expected_artist:
                            e["artist"] = query_expected_artist
                            e["uploader"] = query_expected_artist

                        # Score match accuracy against expected title
                        e_title = (e.get("title") or "").strip().lower()
                        score = 0
                        if e_title == query_expected_title:
                            score = 100
                        elif query_expected_title in e_title:
                            score = 50
                        else:
                            score = 10

                        ytm_entries.append((score, e))

                if ytm_entries:
                    ytm_entries.sort(key=lambda x: x[0], reverse=True)
                    return [e for _, e in ytm_entries]
            except Exception as exc:
                logger.debug("[AI] YTM search fallback for %r: %s", query, exc)

            # 2. Fallback to standard ytsearch if YouTube Music returns no candidates
            search_target = f"ytsearch{fetch_candidates_per_query}:{clean_q}"
            try:
                entries = await loop.run_in_executor(
                    None, _ytdl_flat_search, search_target, fetch_candidates_per_query
                )
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
