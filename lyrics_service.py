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


# Common game publisher, curator, and record label channels whose names are not the primary music artist
PUBLISHER_CHANNELS = {
    'league of legends', 'riot games music', 'riot games', 'valorant',
    'genshin impact', 'hoyoverse', 'honkai: star rail', 'monstercat',
    'ncs', 'no copyright sounds', 'lolesports', 'world of warcraft',
    'overwatch', 'blizzard entertainment', 'apex legends', 'fortnite',
    'cyberpunk 2077', 'cd projekt red'
}

EVENT_KEYWORDS = re.compile(
    r'(?i)\b(worlds?\s*\d*|anthem|champion\s*theme|season\s*\d*|cinematic|original\s*soundtrack|ost|audio|official)\b'
)


def extract_song_candidates(title: str, artist: str = '') -> list[tuple[str, str]]:
    """Extract ordered (artist, title) candidate pairs from a YouTube title and channel name.

    Handles standard 'Artist - Title', featured artists '(ft. Imagine Dragons)',
    and publisher/event compound titles like 'Warriors (ft. Imagine Dragons) | Worlds 2014 - League of Legends'.
    """
    orig_title = str(title or '').strip()
    channel = str(artist or '').strip()
    channel_lower = channel.lower()

    # 1. Extract featured artist inside parens/brackets: (ft. Imagine Dragons) -> Imagine Dragons
    feat_match = re.search(r'(?i)[\(\[]\s*(?:ft|feat|featuring)\.?\s*([^\)\]]+)[\)\]]', orig_title)
    feat_artist = feat_match.group(1).strip() if feat_match else ''

    # 2. Strip video tags and clauses
    cleaned = re.sub(r'\[.*?\]|\(.*?\)', '', orig_title)
    cleaned = re.sub(
        r'(?i)\b(official\s+video|official\s+audio|official\s+music\s+video|lyric\s+video|lyrics|mv|hd|4k|audio|visualizer)\b',
        '',
        cleaned,
    )
    cleaned = re.sub(r'(?i)\b(ft|feat|featuring)\b\.?.*', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    # 3. Split parts by delimiters: ' - ', ' | ', ' // '
    parts = [p.strip() for p in re.split(r'\s*(?: - | \| | // )\s*', cleaned) if p.strip()]

    candidates: list[tuple[str, str]] = []
    seen = set()

    def add_cand(cand_art: str, cand_title: str):
        ca = cand_art.strip()
        ct = cand_title.strip()
        if not ct:
            return
        key = (ca.lower(), ct.lower())
        if key not in seen:
            seen.add(key)
            candidates.append((ca, ct))

    # If featured artist exists, it is very often the actual band/singer performing the song
    if feat_artist and parts:
        add_cand(feat_artist, parts[0])

    is_publisher = any(pub in channel_lower for pub in PUBLISHER_CHANNELS)

    if len(parts) >= 2:
        # Meaningful parts without event noise (e.g. ['Linkin Park', 'Heavy Is the Crown'])
        meaningful_parts = [p for p in parts if not EVENT_KEYWORDS.search(p)]

        if len(meaningful_parts) >= 2:
            add_cand(meaningful_parts[0], meaningful_parts[1])
            add_cand(meaningful_parts[1], meaningful_parts[0])

        # Standard (part 0, part 1) and reverse
        add_cand(parts[0], parts[1])
        add_cand(parts[1], parts[0])

        # If channel is given and not an event tag
        if channel and not EVENT_KEYWORDS.search(channel):
            for mp in (meaningful_parts or parts):
                add_cand(channel, mp)

        # If 3 parts, e.g. 'Warriors | Worlds 2014 - League of Legends'
        if len(parts) >= 3:
            add_cand(parts[-1], parts[0])
            add_cand(parts[0], parts[-1])
    elif parts:
        if channel:
            add_cand(channel, parts[0])
        add_cand('', parts[0])

    return candidates


def clean_song_metadata(title: str, artist: str = ''):
    """Clean YouTube title clutter and extract best-effort (artist, track)."""
    candidates = extract_song_candidates(title, artist)
    if candidates:
        return candidates[0]
    return (str(artist or '').strip(), str(title or '').strip())


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

            # Leading text before first span if present
            if p.text and p.text.strip():
                first_begin = parse_timestamp(span_elements[0].attrib.get('begin', begin)) if span_elements else end
                words.append({
                    'begin': round(begin, 3),
                    'end': round(first_begin, 3),
                    'text': p.text.strip(),
                    'trailing_space': bool(p.text.endswith(' ') or p.text.endswith('\t')),
                })

            for span in span_elements:
                w_begin = parse_timestamp(span.attrib.get('begin', begin))
                w_end = parse_timestamp(span.attrib.get('end', end))
                w_text = ''.join(span.itertext())
                tail = span.tail or ''
                has_space = bool(w_text.endswith(' ') or re.search(r'\s', tail))
                clean_text = w_text.strip()
                if clean_text:
                    words.append({
                        'begin': round(w_begin, 3),
                        'end': round(w_end, 3),
                        'text': clean_text,
                        'trailing_space': has_space,
                    })

            # Align with line_text to guarantee accurate word boundaries
            pos = 0
            for i, w in enumerate(words):
                idx = line_text.find(w['text'], pos)
                if idx != -1:
                    after_pos = idx + len(w['text'])
                    if after_pos < len(line_text):
                        w['trailing_space'] = line_text[after_pos].isspace()
                    else:
                        w['trailing_space'] = True
                    pos = after_pos
                else:
                    if 'trailing_space' not in w:
                        w['trailing_space'] = (i == len(words) - 1)

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


def extract_primary_artist(artist: str) -> str:
    """Extract first primary artist from multi-artist strings."""
    if not artist:
        return ''
    a = artist.replace('，', ',').replace('／', '/').strip()
    parts = re.split(r'[,;/]|\s+(?:x|&|feat\.?|ft\.?)\s+', a, flags=re.IGNORECASE)
    return parts[0].strip() if parts else artist.strip()


def normalize_title_for_comparison(title: str) -> str:
    """Normalize title for fuzzy comparison without brackets or punctuation."""
    if not title:
        return ''
    t = title.lower()
    t = re.sub(r'\[.*?\]|\(.*?\)', '', t)
    t = re.sub(r'[^\w\s]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def fetch_lrclib(song: str, artist: str = '', duration: int = 0):
    """Fetch synced LRC lyrics from LRCLIB API with exact query and fuzzy search fallback."""
    if not song:
        return None

    norm_target = normalize_title_for_comparison(song)
    clean_artist = artist.replace('，', ',').strip()
    primary_artist = extract_primary_artist(artist)

    # 1. Exact match (try full artist, then primary artist)
    artist_candidates = [clean_artist]
    if primary_artist and primary_artist != clean_artist:
        artist_candidates.append(primary_artist)

    for art in artist_candidates:
        if not art:
            continue
        params = {'track_name': song, 'artist_name': art}
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

    # 2. Search fallback with smart candidate scoring (prevents sequels like '3107 3' taking over '3107')
    search_q = f"{primary_artist or clean_artist} {song}".strip() if (primary_artist or clean_artist) else song.strip()
    search_url = f"https://lrclib.net/api/search?{urllib.parse.urlencode({'q': search_q})}"
    try:
        req = urllib.request.Request(search_url, headers={'User-Agent': 'MusicBot/1.0'})
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            results = json.loads(resp.read().decode('utf-8'))
            scored_candidates = []
            for item in results:
                if not item.get('syncedLyrics'):
                    continue
                cand_title = normalize_title_for_comparison(item.get('trackName', ''))
                cand_dur = float(item.get('duration') or 0)
                score = 0

                if cand_title == norm_target:
                    score += 100
                elif norm_target in cand_title:
                    extra = cand_title.replace(norm_target, '').strip()
                    # Heavy penalty if candidate is a numbered sequel (e.g. searching '3107' but cand has '3107 3')
                    if re.search(r'\b\d+\b', extra):
                        score -= 70
                    else:
                        score += 20
                else:
                    score -= 50

                # Duration match bonus
                if duration and duration > 0 and cand_dur > 0:
                    diff = abs(duration - cand_dur)
                    if diff <= 4:
                        score += 40
                    elif diff <= 10:
                        score += 15
                    elif diff > 30:
                        score -= 30

                scored_candidates.append((score, item))

            if scored_candidates:
                scored_candidates.sort(key=lambda x: x[0], reverse=True)
                top_score, top_item = scored_candidates[0]
                if top_score > 0:
                    parsed = parse_lrc(top_item['syncedLyrics'])
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

    # Step 2 & 3: Try candidates with BetterLyrics and LRCLIB
    candidates = extract_song_candidates(title, artist)
    resolved_artist, resolved_title = clean_artist, clean_title

    if not result:
        # First pass: try high-fidelity syllable-level BetterLyrics across all candidate pairs
        for cand_artist, cand_title in candidates:
            result = fetch_betterlyrics(cand_title, cand_artist, duration)
            if result:
                resolved_artist, resolved_title = cand_artist, cand_title
                break

    if not result:
        # Second pass: try line-level LRCLIB across candidate pairs
        for cand_artist, cand_title in candidates:
            result = fetch_lrclib(cand_title, cand_artist, duration)
            if result:
                resolved_artist, resolved_title = cand_artist, cand_title
                break

    # Step 4: Detect MV intro offset via SponsorBlock
    intro_offset = 0.0
    if video_id and result and result.get('provider') != 'unison':
        # Unison is already timed to the videoId, but BetterLyrics/LRCLIB are studio timings.
        # Check if the video has an intro skit before the music starts.
        intro_offset = get_sponsorblock_intro_offset(video_id)

    if result:
        result['intro_offset'] = intro_offset
        result['video_id'] = video_id
        result['track_title'] = resolved_title
        result['track_artist'] = resolved_artist

        # Maintain cache size
        if len(_lyrics_cache) > MAX_CACHE_SIZE:
            oldest_key = min(_lyrics_cache.keys(), key=lambda k: _lyrics_cache[k][1])
            _lyrics_cache.pop(oldest_key, None)

        _lyrics_cache[cache_key] = (result, now)
        return result

    return None
