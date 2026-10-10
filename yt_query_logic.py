import re
from urllib.parse import urlparse, parse_qs


def sanitize_query(query):
    """Extract URL from iframe or embed HTML if provided, else strip whitespace."""
    if not query:
        return ""
    query = str(query).strip()
    iframe_match = re.search(r'src=["\']([^"\']+)["\']', query, re.IGNORECASE)
    if iframe_match:
        return iframe_match.group(1).strip()
    return query


def is_youtube_link(query):
    """Return True when the query is a direct YouTube URL or embed."""
    query = sanitize_query(query).lower()
    return bool(re.match(r'^(https?://)?([a-z0-9-]+\.)?(youtube\.com|youtu\.be)/', query))


def is_probable_url(query):
    """Return True when input looks like an HTTP(S) or www URL."""
    query = sanitize_query(query).lower()
    return bool(re.match(r'^(https?://|www\.)', query))


def normalize_yt_search_term(query):
    """For search terms, just return the stripped query."""
    stripped_query = sanitize_query(query)
    return stripped_query, False


def parse_youtube_url(query):
    """Analyze a YouTube URL to determine if it has a video, playlist, or both.

    Returns a dict:
    {
        'is_youtube': bool,
        'is_playlist': bool,
        'is_pure_playlist': bool,
        'has_video': bool,
        'video_id': str or None,
        'list_id': str or None,
        'single_video_url': str or None,
        'playlist_url': str or None,
        'clean_url': str
    }
    """
    clean = sanitize_query(query)
    if not is_youtube_link(clean):
        return {
            'is_youtube': False,
            'is_playlist': False,
            'is_pure_playlist': False,
            'has_video': False,
            'is_search': False,
            'search_term': None,
            'video_id': None,
            'list_id': None,
            'single_video_url': None,
            'playlist_url': None,
            'clean_url': clean,
        }

    url = clean if clean.startswith(('http://', 'https://')) else f'https://{clean}'
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)

    search_term = None
    is_search = False
    clean_path = parsed.path.rstrip('/')
    if clean_path in ('/search', '/results'):
        terms = qs.get('q') or qs.get('search_query')
        if terms and terms[0]:
            search_term = terms[0].strip()
            is_search = bool(search_term)

    list_id = qs.get('list', [None])[0]

    video_id = None
    if 'v' in qs:
        video_id = qs['v'][0]
    elif 'youtu.be' in parsed.netloc.lower():
        path_parts = parsed.path.lstrip('/').split('/')
        if path_parts and path_parts[0]:
            video_id = path_parts[0]
    elif '/embed/' in parsed.path:
        path_parts = parsed.path.split('/embed/')
        if len(path_parts) > 1:
            video_id = path_parts[1].split('/')[0] or None
    elif '/shorts/' in parsed.path:
        path_parts = parsed.path.split('/shorts/')
        if len(path_parts) > 1:
            video_id = path_parts[1].split('/')[0] or None

    # LL (Liked Videos) and WL (Watch Later) require private account authentication.
    # Standard playlists (PL), albums (OLAK), and YouTube Mixes (RD) are accessible.
    PRIVATE_PLAYLIST_PREFIXES = ('LL', 'WL')
    is_playlist = bool(list_id and not list_id.startswith(PRIVATE_PLAYLIST_PREFIXES))
    has_video = bool(video_id)
    is_pure_playlist = is_playlist and not has_video

    single_video_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else None
    if is_playlist:
        if video_id:
            playlist_url = f"https://www.youtube.com/watch?v={video_id}&list={list_id}"
        else:
            playlist_url = f"https://www.youtube.com/playlist?list={list_id}"
    else:
        playlist_url = None

    return {
        'is_youtube': True,
        'is_playlist': is_playlist,
        'is_pure_playlist': is_pure_playlist,
        'has_video': has_video,
        'is_search': is_search,
        'search_term': search_term,
        'video_id': video_id,
        'list_id': list_id if is_playlist else None,
        'single_video_url': single_video_url,
        'playlist_url': playlist_url,
        'clean_url': url,
    }


def build_yt_music_search_url(query):
    """Construct a YouTube Music search URL for a query string.
    
    Appends '#songs' so yt-dlp triggers YouTube Music's 'Songs' filter tab,
    prioritizing official album/studio tracks over music videos and skits.
    """
    import urllib.parse
    cleaned = sanitize_query(query)
    return f"https://music.youtube.com/search?q={urllib.parse.quote_plus(cleaned)}#songs"


def format_duration(seconds):
    """Format seconds into HH:MM:SS or MM:SS string."""
    if seconds is None:
        return ""
    try:
        total_sec = int(float(seconds))
        if total_sec < 0:
            return ""
        hours = total_sec // 3600
        minutes = (total_sec % 3600) // 60
        secs = total_sec % 60
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"
    except (ValueError, TypeError):
        return ""


