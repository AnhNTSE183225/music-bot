import json
import logging
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

# In-memory LRU/TTL cache for fetched lyrics: { cache_key: (lyrics_data, timestamp) }
_lyrics_cache = {}
CACHE_TTL_SECONDS = 3600  # 1 hour
MAX_CACHE_SIZE = 200


def extract_video_id(url: str):
    """Extract YouTube 11-character video ID from URL if present."""
    if not url:
        return None
    match = re.search(r'(?:v=|\/|youtu\.be\/)([0-9A-Za-z_-]{11})(?:[&?]|$)', str(url))
    return match.group(1) if match else None


def clean_song_metadata(title: str, artist: str = ''):
    """Clean YouTube title clutter and extract best-effort (artist, track)."""
    title = str(title or '').strip()
    artist = str(artist or '').strip()

    # Split sub-titles / anthem tags separated by ' // '
    if ' // ' in title:
        title = title.split(' // ', 1)[0].strip()

    # Remove common video tags: [Official Video], (Audio), (MV), etc.
    cleaned = re.sub(r'\[.*?\]|\(.*?\)', '', title)
    cleaned = re.sub(
        r'(?i)\b(official\s+video|official\s+audio|official\s+music\s+video|lyric\s+video|lyrics|mv|hd|4k|audio|visualizer)\b',
        '',
        cleaned,
    )
    # Remove featuring clauses (e.g. ft. / feat. / featuring)
    cleaned = re.sub(r'(?i)\b(ft|feat|featuring)\b\.?.*', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    # If title has "Artist - Track" or "Artist | Track"
    if ' - ' in cleaned:
        parts = cleaned.split(' - ', 1)
        parsed_artist = parts[0].strip()
        parsed_title = parts[1].strip()
        return (artist or parsed_artist), parsed_title
    elif ' | ' in cleaned:
        parts = cleaned.split(' | ', 1)
        parsed_artist = parts[0].strip()
        parsed_title = parts[1].strip()
        return (artist or parsed_artist), parsed_title

    return artist, cleaned


def parse_timestamp(val):
    """Parse timestamp into float seconds (handles hh:mm:ss.sss, mm:ss.sss, or seconds float)."""
    if val is None:
        return 0.0
    val_str = str(val).strip()
    if ':' in val_str:
        parts = val_str.split(':')
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
    try:
        return float(val_str.rstrip('s'))
    except ValueError:
        return 0.0


def parse_ttml(ttml_xml: str):
    """Parse TTML XML document into unified line and word/syllable timing structure."""
    try:
        root = ET.fromstring(ttml_xml)
        ns = {'tt': 'http://www.w3.org/ns/ttml'}
        body = root.find('tt:body', ns)
        if body is None:
            body = root.find('.//{http://www.w3.org/ns/ttml}body')
        if body is None:
            return None

        p_elements = body.findall('.//{http://www.w3.org/ns/ttml}p')
        if not p_elements:
            p_elements = body.findall('.//p')

        lines = []
        for p in p_elements:
            begin = parse_timestamp(p.attrib.get('begin'))
            end = parse_timestamp(p.attrib.get('end'))
            line_text = ''.join(p.itertext()).strip()
            if not line_text:
                continue

            words = []
            span_elements = p.findall('.//{http://www.w3.org/ns/ttml}span') or p.findall('.//span')
            for span in span_elements:
                w_begin = parse_timestamp(span.attrib.get('begin', begin))
                w_end = parse_timestamp(span.attrib.get('end', end))
                w_text = ''.join(span.itertext())
                if w_text.strip():
                    words.append({
                        'begin': round(w_begin, 3),
                        'end': round(w_end, 3),
                        'text': w_text,
                    })

            lines.append({
                'begin': round(begin, 3),
                'end': round(end, 3),
                'text': line_text,
                'words': words if words else None,
            })

        if not lines:
            return None

        has_syllables = any(l.get('words') for l in lines)
        return {
            'type': 'syllable' if has_syllables else 'line',
            'lines': lines,
        }
    except Exception as e:
        logger.warning("Failed to parse TTML lyrics: %s", e)
        return None


def parse_lrc(lrc_text: str):
    """Parse standard LRC synchronized lyrics into unified line timings."""
    if not lrc_text:
        return None

    pattern = re.compile(r'\[(\d{1,2}:\d{2}(?:\.\d{1,3})?)\](.*)')
    parsed_entries = []

    for line in lrc_text.splitlines():
        line = line.strip()
        match = pattern.match(line)
        if match:
            time_part, text = match.groups()
            sec = parse_timestamp(time_part)
            text = text.strip()
            if text:
                parsed_entries.append((sec, text))

    if not parsed_entries:
        return None

    lines = []
    for i, (sec, text) in enumerate(parsed_entries):
        if i + 1 < len(parsed_entries):
            next_sec = parsed_entries[i + 1][0]
            end_sec = min(next_sec, sec + 8.0)
        else:
            end_sec = sec + 5.0

        lines.append({
            'begin': round(sec, 3),
            'end': round(end_sec, 3),
            'text': text,
            'words': None,
        })

    return {
        'type': 'line',
        'lines': lines,
    }


def get_sponsorblock_intro_offset(video_id: str) -> float:
    """Check SponsorBlock for music_offtopic intro segment (e.g. video dialogue/skit)."""
    if not video_id:
        return 0.0

    url = f"https://sponsor.ajay.app/api/skipSegments?videoID={urllib.parse.quote(video_id)}&categories=[%22music_offtopic%22]"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'MusicBot/1.0'})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            for seg in data:
                start, end = seg.get('segment', [0, 0])
                if start <= 2.0 and end > start:
                    logger.info("Found SponsorBlock MV intro segment for %s: %s -> %s", video_id, start, end)
                    return round(end, 2)
    except Exception:
        pass
    return 0.0


def fetch_unison(video_id: str):
    """Fetch lyrics directly bound to a YouTube videoId from Unison."""
    if not video_id:
        return None

    url = f"https://unison.boidu.dev/lyrics?v={urllib.parse.quote(video_id)}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'MusicBot/1.0'})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            if body.get('success') and body.get('data'):
                data = body['data']
                raw_lyrics = data.get('lyrics', '')
                fmt = str(data.get('format', '')).lower()
                if fmt == 'ttml':
                    parsed = parse_ttml(raw_lyrics)
                else:
                    parsed = parse_lrc(raw_lyrics)

                if parsed and parsed.get('lines'):
                    parsed['provider'] = 'unison'
                    return parsed
    except Exception:
        pass
    return None


def fetch_betterlyrics(song: str, artist: str = '', duration: int = 0):
    """Fetch syllable-level lyrics from BetterLyrics API."""
    if not song:
        return None

    params = {'s': song}
    if artist:
        params['a'] = artist
    if duration and duration > 0:
        params['d'] = int(duration)

    url = f"https://api.betterlyrics.org/getLyrics?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'MusicBot/1.0'})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data.get('ttml'):
                parsed = parse_ttml(data['ttml'])
                if parsed and parsed.get('lines'):
                    parsed['provider'] = 'betterlyrics'
                    return parsed
    except Exception:
        pass
    return None


def fetch_lrclib(song: str, artist: str = '', duration: int = 0):
    """Fetch synced LRC lyrics from LRCLIB API with exact query and fuzzy search fallback."""
    if not song:
        return None

    # 1. Exact match
    params = {'track_name': song}
    if artist:
        params['artist_name'] = artist
    if duration and duration > 0:
        params['duration'] = int(duration)

    url = f"https://lrclib.net/api/get?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'MusicBot/1.0'})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if data.get('syncedLyrics'):
                parsed = parse_lrc(data['syncedLyrics'])
                if parsed and parsed.get('lines'):
                    parsed['provider'] = 'lrclib'
                    return parsed
    except Exception:
        pass

    # 2. Search fallback
    search_q = f"{artist} {song}".strip() if artist else song.strip()
    search_url = f"https://lrclib.net/api/search?{urllib.parse.urlencode({'q': search_q})}"
    try:
        req = urllib.request.Request(search_url, headers={'User-Agent': 'MusicBot/1.0'})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            results = json.loads(resp.read().decode('utf-8'))
            for item in results:
                if item.get('syncedLyrics'):
                    parsed = parse_lrc(item['syncedLyrics'])
                    if parsed and parsed.get('lines'):
                        parsed['provider'] = 'lrclib'
                        return parsed
    except Exception:
        pass

    return None


def get_lyrics_for_song(title: str, artist: str = '', duration: int = 0, url: str = ''):
    """Unified entry point: get synchronized lyrics with multi-provider fallback and intro offset."""
    if not title:
        return None

    video_id = extract_video_id(url)
    clean_artist, clean_title = clean_song_metadata(title, artist)
    cache_key = f"{video_id or ''}:{clean_artist}:{clean_title}"

    # Check cache
    now = time.time()
    if cache_key in _lyrics_cache:
        cached_result, cached_time = _lyrics_cache[cache_key]
        if now - cached_time < CACHE_TTL_SECONDS:
            return cached_result

    # Step 1: Try Unison (videoId-specific sync)
    result = None
    if video_id:
        result = fetch_unison(video_id)

    # Step 2: Try BetterLyrics (syllable-level TTML)
    if not result:
        result = fetch_betterlyrics(clean_title, clean_artist, duration)

    # Step 3: Try LRCLIB (line-level LRC)
    if not result:
        result = fetch_lrclib(clean_title, clean_artist, duration)

    # Step 4: Detect MV intro offset via SponsorBlock
    intro_offset = 0.0
    if video_id and result and result.get('provider') != 'unison':
        # Unison is already timed to the videoId, but BetterLyrics/LRCLIB are studio timings.
        # Check if the video has an intro skit before the music starts.
        intro_offset = get_sponsorblock_intro_offset(video_id)

    if result:
        result['intro_offset'] = intro_offset
        result['video_id'] = video_id
        result['track_title'] = clean_title
        result['track_artist'] = clean_artist

        # Maintain cache size
        if len(_lyrics_cache) > MAX_CACHE_SIZE:
            oldest_key = min(_lyrics_cache.keys(), key=lambda k: _lyrics_cache[k][1])
            _lyrics_cache.pop(oldest_key, None)

        _lyrics_cache[cache_key] = (result, now)
        return result

    return None
