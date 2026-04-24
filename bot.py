import discord
from discord.ext import commands
import os
import asyncio
import yt_dlp
import difflib
import re
from dotenv import load_dotenv
import settings
import logging
import importlib
import math
import time
import secrets
from datetime import datetime, timedelta, timezone
from aiohttp import web

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Also log to file
file_handler = logging.FileHandler(settings.LOG_FILE)
file_handler.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Load Secrets
load_dotenv()
TOKEN = os.getenv(settings.TOKEN_ENV_VAR)

# Validate token on startup
if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable not set. "
        "Please add it to your .env file or system environment."
    )

# --- SETUP ---
intents = discord.Intents.all()

bot = commands.Bot(command_prefix=settings.COMMAND_PREFIX, intents=intents)

ytdl = yt_dlp.YoutubeDL(settings.YTDL_OPTIONS)

# GLOBAL VARIABLES
song_queue = []
current_volume = settings.DEFAULT_VOLUME
play_next_lock = asyncio.Lock()
votes_by_guild = {}
next_queue_id = 1
current_song = None  # Track the currently playing song for status display
current_song_started_perf = None
empty_voice_leave_tasks = {}
EMPTY_VOICE_LEAVE_DELAY_SECONDS = 10
playback_monitor_tasks = {}
loop_lag_monitor_task = None
web_api_runner = None
web_api_site = None
admin_sessions = {}

# Cache blacklist patterns at module level (load once on startup)
_blacklist_patterns = []

def load_yt_blacklist_patterns():
    """Load regex blacklist patterns from config.yaml."""
    patterns = []
    pattern_strings = settings.YT_BLACKLIST_PATTERNS
    
    if not pattern_strings:
        logger.info("No YouTube blacklist patterns configured.")
        return patterns
    
    for raw_pattern in pattern_strings:
        if not raw_pattern or raw_pattern.strip().startswith('#'):
            continue
        try:
            patterns.append(re.compile(raw_pattern.strip(), re.IGNORECASE))
            logger.debug(f"Loaded blacklist pattern: {raw_pattern[:50]}")
        except re.error as e:
            logger.warning(f"Invalid regex pattern in config: {raw_pattern} ({e})")
    
    return patterns

def is_blacklisted_title(title):
    """Return True if the title matches any blacklist regex pattern (uses cached patterns)."""
    for pattern in _blacklist_patterns:
        if pattern.search(title):
            return True
    return False


def log_playback_metric(event_name, **kwargs):
    """Emit structured playback telemetry when debug metrics are enabled."""
    if not settings.PLAYBACK_DEBUG_METRICS:
        return

    fields = [f"event={event_name}"]
    for key, value in kwargs.items():
        if value is None:
            continue
        fields.append(f"{key}={value}")
    logger.info("PLAYBACK_METRIC %s", " ".join(fields))


def cancel_playback_monitor(guild_id):
    """Cancel heartbeat monitor for one guild if active."""
    task = playback_monitor_tasks.pop(guild_id, None)
    if task and not task.done():
        task.cancel()


def start_playback_monitor(ctx, song, playback_started_perf):
    """Start periodic playback heartbeat logs while current song is active."""
    if not ctx.guild:
        return

    guild_id = ctx.guild.id
    cancel_playback_monitor(guild_id)

    queue_id = song.get('queue_id')
    expected_duration_ms = None
    if song.get('duration'):
        expected_duration_ms = int(float(song.get('duration')) * 1000)

    async def monitor():
        try:
            while True:
                await asyncio.sleep(5)

                voice_client = ctx.voice_client
                if not voice_client or not voice_client.is_connected():
                    break

                active_song = current_song
                if not active_song or active_song.get('queue_id') != queue_id:
                    break

                elapsed_ms = int((time.perf_counter() - playback_started_perf) * 1000)
                latency_ms = int(getattr(voice_client, 'latency', 0.0) * 1000)
                average_latency_ms = int(getattr(voice_client, 'average_latency', 0.0) * 1000)
                state = 'playing' if voice_client.is_playing() else ('paused' if voice_client.is_paused() else 'idle')

                log_playback_metric(
                    "playback_heartbeat",
                    guild_id=guild_id,
                    queue_id=queue_id,
                    source_type=song.get('type'),
                    elapsed_ms=elapsed_ms,
                    expected_duration_ms=expected_duration_ms,
                    state=state,
                    latency_ms=latency_ms,
                    avg_latency_ms=average_latency_ms,
                )
        except asyncio.CancelledError:
            return
        except Exception as monitor_exc:
            logger.warning("Playback monitor failed in guild %s: %s", guild_id, monitor_exc)
        finally:
            playback_monitor_tasks.pop(guild_id, None)

    playback_monitor_tasks[guild_id] = bot.loop.create_task(monitor())


def ensure_loop_lag_monitor():
    """Start a lightweight event-loop lag monitor once."""
    global loop_lag_monitor_task
    if loop_lag_monitor_task and not loop_lag_monitor_task.done():
        return

    async def monitor_loop_lag():
        interval = 1.0
        threshold_ms = 200
        expected = time.perf_counter() + interval
        while True:
            await asyncio.sleep(interval)
            now = time.perf_counter()
            lag_ms = int((now - expected) * 1000)
            if lag_ms > threshold_ms:
                log_playback_metric("event_loop_lag", lag_ms=lag_ms)
            expected = now + interval

    loop_lag_monitor_task = bot.loop.create_task(monitor_loop_lag())

async def update_bot_status(song_title):
    """Update bot's status to show currently playing song."""
    try:
        activity = discord.Activity(
            type=discord.ActivityType.playing,
            name=song_title
        )
        await bot.change_presence(activity=activity)
        logger.debug(f"Bot status updated to: {song_title}")
    except Exception as e:
        logger.warning(f"Failed to update bot status: {e}")

async def clear_bot_status():
    """Clear bot's status back to default (no activity)."""
    try:
        await bot.change_presence(activity=None)
        logger.debug("Bot status cleared")
    except Exception as e:
        logger.warning(f"Failed to clear bot status: {e}")

def find_best_match(query):
    """Smart search for local files."""
    if not os.path.exists(settings.MEDIA_FOLDER):
        os.makedirs(settings.MEDIA_FOLDER)
        return None

    files = [f for f in os.listdir(settings.MEDIA_FOLDER) if f.endswith(('.mp3', '.mp4'))]
    query = query.lower()

    # 1. Exact Match
    for f in files:
        if query == f.lower(): return f
    # 2. Partial Match
    for f in files:
        if query in f.lower(): return f
    # 3. Fuzzy Match
    close_matches = difflib.get_close_matches(query, files, n=1, cutoff=0.5)
    return close_matches[0] if close_matches else None


def build_song_payload(song):
    """Convert an internal song object into API-safe JSON payload."""
    if not song:
        return None

    thumbnail_url = song.get('thumbnail') or song.get('thumbnail_url')
    if not thumbnail_url and song.get('webpage_url'):
        thumbnail_url = f"https://img.youtube.com/vi/{song.get('webpage_url').split('v=')[-1].split('&')[0]}/hqdefault.jpg"

    return {
        'id': str(song.get('queue_id', '')),
        'title': song.get('title', 'Unknown track'),
        'artist': song.get('uploader', 'YouTube') if song.get('type') == 'youtube' else 'Local file',
        'durationSec': int(song.get('duration') or 0),
        'thumbnailUrl': thumbnail_url or '',
        'requestedBy': song.get('requester_handle', 'unknown'),
        'sourceUrl': song.get('webpage_url') or song.get('data') or '',
    }


def get_primary_voice_client():
    """Return the first connected voice client, if any."""
    for voice_client in bot.voice_clients:
        if voice_client and voice_client.is_connected():
            return voice_client
    return None


class WebPlaybackContext:
    """Minimal playback context for API-triggered queue operations."""

    def __init__(self, voice_client):
        self.voice_client = voice_client
        self.guild = getattr(voice_client, 'guild', None)

    async def send(self, _message):
        # API calls should not post bot messages in text channels.
        return


def get_vote_required_for_voice_client(voice_client):
    """Compute skip votes required using current config and live listeners."""
    vote_cfg = settings.get_skip_vote_config()
    same_channel_only = vote_cfg.get('same_channel_only', True)

    if not voice_client or not voice_client.guild:
        return 1, 0

    if same_channel_only:
        eligible_members = [m for m in voice_client.channel.members if not m.bot] if voice_client.channel else []
    else:
        eligible_members = [
            m for m in voice_client.guild.members if (not m.bot and m.voice and m.voice.channel)
        ]

    member_count = len(eligible_members)
    if member_count == 0:
        return 1, 0

    threshold_type = str(vote_cfg.get('threshold_type', 'ratio')).lower()
    threshold_value = vote_cfg.get('threshold_value', 0.5)
    min_votes = max(1, int(vote_cfg.get('min_votes', 1)))

    if threshold_type == 'absolute':
        base_required = max(1, int(threshold_value))
    else:
        base_required = math.ceil(member_count * float(threshold_value))

    required = max(min_votes, base_required)
    required = min(required, member_count)
    return required, member_count


async def extract_youtube_video_data(query):
    """Resolve a user query/url to a single YouTube video data object."""
    if query.startswith(("http://", "https://", "www.")):
        search_query = query
    else:
        search_query = f"ytsearch:{query}"

    data = await asyncio.to_thread(ytdl.extract_info, search_query, False)
    if not data:
        raise RuntimeError("No data returned from YouTube lookup")

    if 'entries' in data:
        entries = data.get('entries') or []
        if not entries:
            raise RuntimeError("No results found")
        return entries[0]

    return data


def make_web_song(video_data, requester_label):
    """Create a queue entry compatible with existing playback pipeline from web input."""
    global next_queue_id

    webpage_url = video_data.get('webpage_url') or video_data.get('url')
    title = video_data.get('title') or 'Untitled YouTube Track'

    song = {
        'queue_id': next_queue_id,
        'type': 'youtube',
        'title': title,
        'data': webpage_url,
        'requester_id': 0,
        'requester_mention': requester_label,
        'requester_handle': requester_label,
        'enqueued_perf': time.perf_counter(),
        'stream_url': video_data.get('url'),
        'stream_url_cached_at': time.time() if video_data.get('url') else None,
        'format_id': video_data.get('format_id'),
        'ext': video_data.get('ext'),
        'duration': video_data.get('duration'),
        'uploader': video_data.get('uploader') or video_data.get('channel'),
        'thumbnail': video_data.get('thumbnail'),
        'webpage_url': webpage_url,
    }
    next_queue_id += 1
    return song


def build_api_snapshot():
    """Build a frontend snapshot from live bot state."""
    voice_client = get_primary_voice_client()
    listeners = get_non_bot_voice_member_count(voice_client)
    required_votes, _ = get_vote_required_for_voice_client(voice_client)
    guild_id = voice_client.guild.id if voice_client and voice_client.guild else 0
    guild_votes = votes_by_guild.get(guild_id, {}) if guild_id else {}
    skip_votes = len(guild_votes.get('skip', set()))

    elapsed_sec = 0
    if current_song_started_perf is not None:
        elapsed_sec = max(0, int(time.perf_counter() - current_song_started_perf))

    now_payload = build_song_payload(current_song)
    if now_payload is None:
        now_payload = {
            'id': 'none',
            'title': 'No song is playing',
            'artist': 'MusicBot',
            'durationSec': 0,
            'thumbnailUrl': '',
            'requestedBy': 'system',
            'sourceUrl': '',
        }

    return {
        'roomId': str(guild_id or 'offline'),
        'listeners': listeners,
        'volume': int(current_volume * 100),
        'voteSkip': {
            'count': skip_votes,
            'threshold': required_votes,
        },
        'nowPlaying': {
            'track': now_payload,
            'elapsedSec': elapsed_sec,
        },
        'queue': [build_song_payload(song) for song in song_queue],
        'isConnected': bool(voice_client and voice_client.is_connected()),
    }


def build_cors_headers(request):
    """Build CORS headers for allowed frontend origins."""
    origin = request.headers.get('Origin')
    if origin in settings.WEB_API_ALLOWED_ORIGINS:
        requested_headers = request.headers.get('Access-Control-Request-Headers', '')
        allow_headers = {'Content-Type', 'Authorization', 'X-MusicBot-Admin-Token'}
        if requested_headers:
            for header in requested_headers.split(','):
                name = header.strip()
                if name:
                    allow_headers.add(name)

        return {
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
            'Access-Control-Allow-Headers': ', '.join(sorted(allow_headers, key=str.lower)),
            'Access-Control-Max-Age': '600',
            'Vary': 'Origin, Access-Control-Request-Headers',
        }
    return {}


def json_with_cors(request, payload, status=200):
    headers = build_cors_headers(request)
    return web.json_response(payload, status=status, headers=headers)


def _extract_bearer_token(request):
    authorization = request.headers.get('Authorization', '')
    if authorization.startswith('Bearer '):
        return authorization.removeprefix('Bearer ').strip()
    return request.headers.get('X-MusicBot-Admin-Token', '').strip()


def _get_admin_session(request):
    token = _extract_bearer_token(request)
    if not token:
        return None

    session = admin_sessions.get(token)
    if not session:
        return None

    if session['expires_at'] < time.time():
        admin_sessions.pop(token, None)
        return None

    return session


def _require_admin(request):
    session = _get_admin_session(request)
    if not session:
        return None, json_with_cors(request, {'error': 'Admin login required'}, status=401)
    return session, None


def _issue_admin_token(username):
    token = secrets.token_urlsafe(32)
    admin_sessions[token] = {
        'username': username,
        'expires_at': time.time() + settings.WEB_ADMIN_TOKEN_TTL_SECONDS,
    }
    return token


async def api_login(request):
    body = await request.json()
    username = str(body.get('username') or '').strip()
    password = str(body.get('password') or '').strip()

    if not settings.WEB_ADMIN_USERNAME or not settings.WEB_ADMIN_PASSWORD:
        return json_with_cors(request, {'error': 'Web admin credentials are not configured'}, status=500)

    if username != settings.WEB_ADMIN_USERNAME or password != settings.WEB_ADMIN_PASSWORD:
        return json_with_cors(request, {'error': 'Invalid username or password'}, status=401)

    token = _issue_admin_token(username)
    return json_with_cors(
        request,
        {
            'token': token,
            'user': {
                'username': username,
                'role': 'admin',
            },
        },
    )


async def api_session(request):
    session = _get_admin_session(request)
    if not session:
        return json_with_cors(request, {'authenticated': False, 'user': None})

    return json_with_cors(
        request,
        {
            'authenticated': True,
            'user': {
                'username': session['username'],
                'role': 'admin',
            },
        },
    )


async def api_logout(request):
    token = _extract_bearer_token(request)
    if token:
        admin_sessions.pop(token, None)
    return json_with_cors(request, {'loggedOut': True})


async def api_options(request):
    return web.Response(status=204, headers=build_cors_headers(request))


async def api_get_state(request):
    return json_with_cors(request, build_api_snapshot())


async def api_set_volume(request):
    global current_volume

    session, response = _require_admin(request)
    if response:
        return response

    body = await request.json()
    volume = int(body.get('volume', -1))
    if volume < 0 or volume > 100:
        return json_with_cors(request, {'error': 'Volume must be between 0 and 100'}, status=400)

    current_volume = volume / 100
    voice_client = get_primary_voice_client()
    if voice_client and voice_client.source:
        voice_client.source.volume = current_volume

    return json_with_cors(request, {'snapshot': build_api_snapshot(), 'user': session['username']})


async def api_vote_skip(request):
    body = await request.json()
    voter_id = str(body.get('voterId') or '').strip()
    if not voter_id:
        return json_with_cors(request, {'error': 'voterId is required'}, status=400)

    voice_client = get_primary_voice_client()
    if not voice_client or not voice_client.is_playing():
        return json_with_cors(request, {'error': 'Nothing is playing'}, status=400)

    guild_id = voice_client.guild.id
    votes, already_voted = register_vote(guild_id, 'skip', voter_id)
    required_votes, eligible_count = get_vote_required_for_voice_client(voice_client)

    if already_voted:
        return json_with_cors(
            request,
            {
                'alreadyVoted': True,
                'count': len(votes),
                'threshold': required_votes,
                'eligible': eligible_count,
                'snapshot': build_api_snapshot(),
            },
        )

    skipped = False
    if len(votes) >= required_votes:
        clear_votes(guild_id, action_key='skip')
        voice_client.stop()
        skipped = True

    return json_with_cors(
        request,
        {
            'alreadyVoted': False,
            'count': len(votes),
            'threshold': required_votes,
            'eligible': eligible_count,
            'skipped': skipped,
            'snapshot': build_api_snapshot(),
        },
    )


async def api_search_youtube(request):
    query = (request.query.get('q') or '').strip()
    if not query:
        return json_with_cors(request, {'results': []})

    search_query = f"ytsearch8:{query}"
    data = await asyncio.to_thread(ytdl.extract_info, search_query, False)
    entries = (data or {}).get('entries') or []

    results = []
    for entry in entries:
        webpage_url = entry.get('webpage_url') or entry.get('url')
        if not webpage_url:
            continue
        results.append(
            {
                'title': entry.get('title') or 'Untitled',
                'url': webpage_url,
                'durationSec': int(entry.get('duration') or 0),
                'thumbnailUrl': entry.get('thumbnail') or '',
                'channel': entry.get('uploader') or entry.get('channel') or 'YouTube',
            }
        )

    return json_with_cors(request, {'results': results})


async def api_add_youtube_to_queue(request):
    body = await request.json()
    query = str(body.get('query') or '').strip()
    requester = str(body.get('requester') or 'Web User').strip()
    if not query:
        return json_with_cors(request, {'error': 'query is required'}, status=400)

    try:
        video_data = await extract_youtube_video_data(query)
    except Exception as e:
        return json_with_cors(request, {'error': f'Failed to resolve YouTube query: {e}'}, status=400)

    title = video_data.get('title') or ''
    if is_blacklisted_title(title):
        return json_with_cors(request, {'error': 'This song is in the blacklist'}, status=400)

    song = make_web_song(video_data, requester)
    song_queue.append(song)

    voice_client = get_primary_voice_client()
    if voice_client and voice_client.is_connected() and not voice_client.is_playing() and not voice_client.is_paused():
        async with play_next_lock:
            await play_next(WebPlaybackContext(voice_client))

    return json_with_cors(request, {'queued': True, 'song': build_song_payload(song), 'snapshot': build_api_snapshot()})


async def api_admin_set_volume(request):
    global current_volume

    session, response = _require_admin(request)
    if response:
        return response

    body = await request.json()
    volume = int(body.get('volume', -1))
    if volume < 0 or volume > 100:
        return json_with_cors(request, {'error': 'Volume must be between 0 and 100'}, status=400)

    current_volume = volume / 100
    voice_client = get_primary_voice_client()
    if voice_client and voice_client.source:
        voice_client.source.volume = current_volume

    return json_with_cors(request, {'snapshot': build_api_snapshot(), 'user': session['username']})


async def api_admin_clear(request):
    session, response = _require_admin(request)
    if response:
        return response

    voice_client = get_primary_voice_client()
    if voice_client and voice_client.is_connected():
        clear_votes(voice_client.guild.id)

    song_queue.clear()
    return json_with_cors(request, {'snapshot': build_api_snapshot(), 'user': session['username']})


async def api_admin_skip(request):
    session, response = _require_admin(request)
    if response:
        return response

    voice_client = get_primary_voice_client()
    if not voice_client or not voice_client.is_playing():
        return json_with_cors(request, {'error': 'Nothing is playing'}, status=400)

    if voice_client.guild:
        clear_votes(voice_client.guild.id, action_key='skip')
    voice_client.stop()
    return json_with_cors(request, {'snapshot': build_api_snapshot(), 'user': session['username']})


async def api_admin_stop(request):
    session, response = _require_admin(request)
    if response:
        return response

    voice_client = get_primary_voice_client()
    if voice_client and voice_client.guild:
        cancel_empty_voice_leave_timer(voice_client.guild.id)
        clear_votes(voice_client.guild.id)
        cancel_playback_monitor(voice_client.guild.id)

    global current_song, current_song_started_perf
    current_song = None
    current_song_started_perf = None
    song_queue.clear()

    if voice_client:
        voice_client.stop()
        await voice_client.disconnect()
        await clear_bot_status()

    return json_with_cors(request, {'snapshot': build_api_snapshot(), 'user': session['username']})


async def api_admin_skipto(request):
    session, response = _require_admin(request)
    if response:
        return response

    body = await request.json()
    index = int(body.get('index', 0))
    voice_client = get_primary_voice_client()

    if not voice_client or not voice_client.is_playing():
        return json_with_cors(request, {'error': 'Nothing is playing'}, status=400)

    if index < 1 or index > len(song_queue):
        return json_with_cors(request, {'error': f'Invalid position. Choose between 1 and {len(song_queue)}.'}, status=400)

    song_queue[:] = song_queue[index - 1:]
    voice_client.stop()
    return json_with_cors(request, {'snapshot': build_api_snapshot(), 'user': session['username']})


async def api_admin_remove(request):
    session, response = _require_admin(request)
    if response:
        return response

    body = await request.json()
    index = int(body.get('index', 0))

    if index < 1 or index > len(song_queue):
        return json_with_cors(request, {'error': f'Invalid position. Choose between 1 and {len(song_queue)}.'}, status=400)

    removed_song = song_queue.pop(index - 1)
    if request.app:
        voice_client = get_primary_voice_client()
        if voice_client and voice_client.guild:
            clear_votes(voice_client.guild.id, action_key=f"remove:{removed_song['queue_id']}")

    return json_with_cors(request, {'removed': True, 'snapshot': build_api_snapshot(), 'user': session['username']})


async def start_web_api():
    """Start the HTTP API server for frontend integration."""
    global web_api_runner, web_api_site
    if not settings.WEB_API_ENABLED:
        logger.info("Web API disabled by config.")
        return

    if web_api_runner is not None:
        return

    app = web.Application()
    app.add_routes(
        [
            web.post('/api/auth/login', api_login),
            web.get('/api/auth/session', api_session),
            web.post('/api/auth/logout', api_logout),
            web.get('/api/state', api_get_state),
            web.post('/api/volume', api_set_volume),
            web.post('/api/skip-vote', api_vote_skip),
            web.get('/api/youtube/search', api_search_youtube),
            web.post('/api/queue/youtube', api_add_youtube_to_queue),
            web.post('/api/admin/volume', api_admin_set_volume),
            web.post('/api/admin/clear', api_admin_clear),
            web.post('/api/admin/skip', api_admin_skip),
            web.post('/api/admin/stop', api_admin_stop),
            web.post('/api/admin/skipto', api_admin_skipto),
            web.post('/api/admin/remove', api_admin_remove),
            web.options('/api/{tail:.*}', api_options),
        ]
    )

    web_api_runner = web.AppRunner(app)
    await web_api_runner.setup()
    web_api_site = web.TCPSite(web_api_runner, settings.WEB_API_HOST, settings.WEB_API_PORT)
    await web_api_site.start()
    logger.info("Web API started at http://%s:%s", settings.WEB_API_HOST, settings.WEB_API_PORT)

async def ensure_voice_connected(ctx):
    """Ensure bot is connected to user's voice channel. Returns True on success."""
    if ctx.voice_client and ctx.voice_client.is_connected():
        return True
    
    if not ctx.author.voice:
        await ctx.send("❌ You must be in a voice channel first.")
        return False
    
    try:
        started = time.perf_counter()
        await ctx.author.voice.channel.connect(timeout=settings.CONNECTION_TIMEOUT, reconnect=True)
        await asyncio.sleep(settings.CONNECTION_STABILIZE_DELAY)
        elapsed = time.perf_counter() - started
        log_playback_metric(
            "voice_connect_ready",
            guild_id=getattr(ctx.guild, 'id', None),
            elapsed_ms=int(elapsed * 1000),
            stabilize_delay_ms=int(settings.CONNECTION_STABILIZE_DELAY * 1000),
        )
        return True
    except Exception as e:
        logger.error(f"Failed to connect to voice channel: {e}")
        await ctx.send(f"❌ Failed to connect: {e}")
        return False


def make_song(song_type, title, data, requester):
    """Create a queue song object with requester metadata."""
    global next_queue_id
    song = {
        'queue_id': next_queue_id,
        'type': song_type,
        'title': title,
        'data': data,
        'requester_id': requester.id,
        'requester_mention': requester.mention,
        'requester_handle': str(requester),
        'enqueued_perf': time.perf_counter(),
    }
    next_queue_id += 1
    return song


def is_admin_member(member):
    """Return True if the Discord member has Administrator permission."""
    return bool(getattr(member.guild_permissions, 'administrator', False))


def get_command_mode(command_name):
    """Get normalized command permission mode from config."""
    mode = settings.get_command_permission_mode(command_name)
    valid_modes = {'open', 'admin_only', 'vote_if_non_admin'}
    if mode not in valid_modes:
        logger.warning(f"Invalid mode '{mode}' for command '{command_name}', using 'open'")
        return 'open'
    return mode


async def enforce_command_access(ctx, command_name):
    """Return True if user can execute command immediately."""
    mode = get_command_mode(command_name)
    if mode == 'open' or is_admin_member(ctx.author):
        return True
    if mode == 'admin_only':
        await ctx.send("❌ Only administrators can use this command.")
        return False
    return True


class BlockedUserError(commands.CheckFailure):
    """Raised when a blocked user attempts to run a command."""


class WhitelistOnlyError(commands.CheckFailure):
    """Raised when whitelist mode is enabled and user is not in whitelist."""


@bot.check
def enforce_user_access(ctx):
    """Enforce global access policy.

    Rules:
      - If whitelist has entries, only those users are allowed.
      - Otherwise, allow everyone except blocked users.
    """
    if not ctx.author:
        return True

    allowed_ids = settings.get_allowed_user_ids()
    if allowed_ids:
        if ctx.author.id not in allowed_ids:
            raise WhitelistOnlyError(f"User {ctx.author.id} is not in whitelist")
        return True

    if ctx.author.id in settings.get_blocked_user_ids():
        raise BlockedUserError(f"User {ctx.author.id} is blocked")

    return True


def clear_votes(guild_id, action_key=None):
    """Clear votes for one guild, or one action if action_key is provided."""
    if action_key is None:
        votes_by_guild.pop(guild_id, None)
        return

    guild_votes = votes_by_guild.get(guild_id)
    if not guild_votes:
        return

    guild_votes.pop(action_key, None)
    if not guild_votes:
        votes_by_guild.pop(guild_id, None)


def get_non_bot_voice_member_count(voice_client):
    """Count non-bot users in the bot's current voice channel."""
    if not voice_client or not voice_client.channel:
        return 0
    return sum(1 for member in voice_client.channel.members if not member.bot)


def cancel_empty_voice_leave_timer(guild_id):
    """Cancel pending leave timer for one guild if it exists."""
    task = empty_voice_leave_tasks.pop(guild_id, None)
    if task and not task.done():
        task.cancel()


def ensure_empty_voice_leave_timer(guild):
    """Start or stop leave timer depending on whether humans are in voice."""
    if not guild:
        return

    voice_client = guild.voice_client
    guild_id = guild.id

    if not voice_client or not voice_client.is_connected() or get_non_bot_voice_member_count(voice_client) > 0:
        cancel_empty_voice_leave_timer(guild_id)
        return

    existing = empty_voice_leave_tasks.get(guild_id)
    if existing and not existing.done():
        return

    async def leave_if_still_empty():
        global current_song, current_song_started_perf
        try:
            await asyncio.sleep(EMPTY_VOICE_LEAVE_DELAY_SECONDS)
            fresh_voice_client = guild.voice_client
            if not fresh_voice_client or not fresh_voice_client.is_connected():
                return
            if get_non_bot_voice_member_count(fresh_voice_client) > 0:
                return

            current_song = None
            current_song_started_perf = None
            song_queue.clear()
            clear_votes(guild_id)
            fresh_voice_client.stop()
            await fresh_voice_client.disconnect()
            await clear_bot_status()
            logger.info(
                "Disconnected from voice in guild %s after %s seconds with no listeners.",
                guild_id,
                EMPTY_VOICE_LEAVE_DELAY_SECONDS,
            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("Failed auto-leave in guild %s: %s", guild_id, e)
        finally:
            empty_voice_leave_tasks.pop(guild_id, None)

    empty_voice_leave_tasks[guild_id] = bot.loop.create_task(leave_if_still_empty())


def get_skip_vote_eligible_members(ctx, same_channel_only):
    """Get members eligible to participate in skip voting."""
    if not ctx.guild:
        return []

    if same_channel_only:
        if not ctx.voice_client or not ctx.voice_client.channel:
            return []
        return [m for m in ctx.voice_client.channel.members if not m.bot]

    return [m for m in ctx.guild.members if (not m.bot and m.voice and m.voice.channel)]


def get_skip_vote_required_count(ctx):
    """Calculate required votes from config and current eligible members."""
    vote_cfg = settings.get_skip_vote_config()
    eligible_members = get_skip_vote_eligible_members(ctx, vote_cfg['same_channel_only'])
    member_count = len(eligible_members)

    if member_count == 0:
        return 1, 0

    threshold_type = str(vote_cfg['threshold_type']).lower()
    threshold_value = vote_cfg['threshold_value']
    min_votes = max(1, int(vote_cfg['min_votes']))

    if threshold_type == 'absolute':
        base_required = max(1, int(threshold_value))
    else:
        ratio = float(threshold_value)
        base_required = math.ceil(member_count * ratio)

    required = max(min_votes, base_required)
    required = min(required, member_count)
    return required, member_count


def register_vote(guild_id, action_key, user_id):
    """Register one vote for a guild/action and return the updated vote set."""
    guild_votes = votes_by_guild.setdefault(guild_id, {})
    votes = guild_votes.setdefault(action_key, set())
    already_voted = user_id in votes
    votes.add(user_id)
    return votes, already_voted


def validate_command_permissions_config():
    """Ensure every registered command has an explicit permissions config entry."""
    permissions_cfg = settings.get_permissions_config()
    commands_cfg = permissions_cfg.get('commands', {}) or {}

    configured_commands = set(commands_cfg.keys())
    registered_commands = {cmd.name for cmd in bot.commands}

    missing = sorted(registered_commands - configured_commands)
    if missing:
        raise RuntimeError(
            "Missing permissions.commands entries in config.yaml for: "
            + ", ".join(missing)
        )

    extra = sorted(configured_commands - registered_commands)
    if extra:
        logger.warning(
            "permissions.commands has extra entries not registered in bot: %s",
            ", ".join(extra)
        )

async def play_next(ctx):
    """Plays the next item in the queue with volume control. Must be called within play_next_lock."""
    global current_song, current_song_started_perf

    # Check if voice client exists and is connected
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        logger.debug("Voice client not connected, cannot play next song")
        return

    # Guard against concurrent starters.
    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        return

    if not song_queue:
        current_song = None
        current_song_started_perf = None
        await clear_bot_status()
        return

    song = song_queue.pop(0)
    current_song = song  # Track currently playing song
    queue_wait_ms = int((time.perf_counter() - song.get('enqueued_perf', time.perf_counter())) * 1000)

    try:
        logger.debug(f"Now playing - {song['type']}: {song['title']}")
        # 1. Create the base Source
        if song['type'] == 'local':
            source_path = os.path.join(settings.MEDIA_FOLDER, song['data'])
            source_init_start = time.perf_counter()
            source = discord.FFmpegPCMAudio(source_path)
            source_init_ms = int((time.perf_counter() - source_init_start) * 1000)
            log_playback_metric(
                "source_created",
                queue_id=song.get('queue_id'),
                source_type='local',
                init_ms=source_init_ms,
                queue_wait_ms=queue_wait_ms,
            )
        elif song['type'] == 'youtube':
            logger.debug(f"Creating FFmpeg source for YouTube: {song['data']}")

            data = None
            cache_age_sec = None
            cached_stream_url = song.get('stream_url')
            cached_at = song.get('stream_url_cached_at')
            if cached_stream_url and cached_at:
                cache_age_sec = time.time() - float(cached_at)
                if cache_age_sec <= settings.YT_STREAM_CACHE_TTL_SECONDS:
                    data = {
                        'url': cached_stream_url,
                        'format_id': song.get('format_id'),
                        'ext': song.get('ext'),
                        'duration': song.get('duration'),
                    }
                    log_playback_metric(
                        "yt_stream_cache_hit",
                        queue_id=song.get('queue_id'),
                        cache_age_ms=int(cache_age_sec * 1000),
                    )

            if data is None:
                loop = asyncio.get_event_loop()
                yt_extract_start = time.perf_counter()
                data = await loop.run_in_executor(None, lambda: ytdl.extract_info(song['data'], download=False))
                yt_extract_ms = int((time.perf_counter() - yt_extract_start) * 1000)
                log_playback_metric(
                    "yt_stream_extract",
                    queue_id=song.get('queue_id'),
                    extract_ms=yt_extract_ms,
                    cache_age_ms=(int(cache_age_sec * 1000) if cache_age_sec is not None else None),
                )

            if 'entries' in data:
                data = data['entries'][0]

            filename = data['url']
            if not filename:
                raise ValueError("YouTube extractor returned empty stream URL")

            song['stream_url'] = filename
            song['stream_url_cached_at'] = time.time()
            song['format_id'] = data.get('format_id')
            song['ext'] = data.get('ext')
            song['duration'] = data.get('duration')

            logger.debug(f"Stream URL obtained: {filename[:100]}...")
            logger.debug(f"Format: {data.get('format_id')}, ext: {data.get('ext')}")

            source_init_start = time.perf_counter()
            source = discord.FFmpegPCMAudio(filename, **settings.FFMPEG_OPTIONS)
            source_init_ms = int((time.perf_counter() - source_init_start) * 1000)
            log_playback_metric(
                "source_created",
                queue_id=song.get('queue_id'),
                source_type='youtube',
                init_ms=source_init_ms,
                queue_wait_ms=queue_wait_ms,
                format_id=song.get('format_id'),
                ext=song.get('ext'),
            )
        elif song['type'] == 'url':
            logger.debug(f"Creating FFmpeg source for generic URL: {song['data']}")
            source_init_start = time.perf_counter()
            source = discord.FFmpegPCMAudio(
                song['data'],
                **settings.FFMPEG_OPTIONS,
            )
            source_init_ms = int((time.perf_counter() - source_init_start) * 1000)
            log_playback_metric(
                "source_created",
                queue_id=song.get('queue_id'),
                source_type='url',
                init_ms=source_init_ms,
                queue_wait_ms=queue_wait_ms,
            )
        else:
            raise ValueError(f"Unknown song type: {song['type']}")

        # 2. Apply Volume Transformer
        source = discord.PCMVolumeTransformer(source)
        source.volume = current_volume

        # 3. Play - double check connection and playback state before playing
        if ctx.voice_client and ctx.voice_client.is_connected():
            if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
                # Another call started playback while we were preparing this source.
                song_queue.insert(0, song)
                return

            playback_started = time.perf_counter()
            current_song_started_perf = playback_started
            start_playback_monitor(ctx, song, playback_started)

            def after_playback(error):
                """Called after playback ends. Schedules next song with proper lock protection."""
                elapsed_ms = int((time.perf_counter() - playback_started) * 1000)
                drift_ms = None
                if song.get('duration'):
                    drift_ms = elapsed_ms - int(float(song.get('duration')) * 1000)
                log_playback_metric(
                    "playback_finished",
                    queue_id=song.get('queue_id'),
                    source_type=song.get('type'),
                    elapsed_ms=elapsed_ms,
                    duration_sec=song.get('duration'),
                    drift_ms=drift_ms,
                    error=(str(error)[:200] if error else None),
                )
                if error:
                    logger.error(f"Playback callback error: {error}")
                if ctx.guild:
                    cancel_playback_monitor(ctx.guild.id)
                # Schedule play_next with lock to prevent race conditions
                async def next_with_lock():
                    lock_wait_start = time.perf_counter()
                    async with play_next_lock:
                        lock_wait_ms = int((time.perf_counter() - lock_wait_start) * 1000)
                        if lock_wait_ms >= 100:
                            log_playback_metric(
                                "play_next_lock_wait",
                                queue_id=song.get('queue_id'),
                                wait_ms=lock_wait_ms,
                            )
                        if ctx.voice_client and ctx.voice_client.is_connected():
                            await play_next(ctx)

                future = asyncio.run_coroutine_threadsafe(next_with_lock(), bot.loop)

                def _on_future_done(done_future):
                    try:
                        done_future.result()
                    except Exception as callback_exc:
                        logger.error("play_next callback task failed: %s", callback_exc, exc_info=True)

                future.add_done_callback(_on_future_done)

            if ctx.guild:
                clear_votes(ctx.guild.id, action_key='skip')
            ctx.voice_client.play(source, after=after_playback)

            log_playback_metric(
                "playback_started",
                queue_id=song.get('queue_id'),
                source_type=song.get('type'),
                queue_wait_ms=queue_wait_ms,
                queue_size_after_pop=len(song_queue),
                guild_id=getattr(ctx.guild, 'id', None),
            )
            
            # Update bot's status to show currently playing song
            await update_bot_status(song['title'])
            
            await ctx.send(
                f"🎶 **Now Playing:** {song['title']} "
                f"(requested by {song.get('requester_mention', 'unknown')}, Vol: {int(current_volume * 100)}%)"
            )
        else:
            logger.warning("Lost connection before playing")
            song_queue.insert(0, song)  # Put song back in queue
            await ctx.send("❌ Lost voice connection")

    except Exception as e:
        logger.error(f"Error in play_next: {e}", exc_info=True)
        await ctx.send(f"❌ Error playing: {e}")
        # Try next song after a small delay
        await asyncio.sleep(1)
        if ctx.voice_client and ctx.voice_client.is_connected():
            async with play_next_lock:
                await play_next(ctx)

# --- EVENTS ---

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user}')

@bot.event
async def on_command_error(ctx, error):
    """Handles permission errors nicely."""
    if isinstance(error, commands.CommandNotFound):
        pass  # Ignore invalid commands
    elif isinstance(error, BlockedUserError):
        await ctx.send("❌ You are blocked from using this bot.")
        logger.info("Blocked user %s attempted command '%s'", ctx.author.id, ctx.message.content)
    elif isinstance(error, WhitelistOnlyError):
        await ctx.send("❌ You are not allowed to use this bot.")
        logger.info("Non-whitelisted user %s attempted command '%s'", ctx.author.id, ctx.message.content)
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument provided. {error}")
    else:
        logger.error(f"Command error in {ctx.command}: {error}", exc_info=True)
        await ctx.send(f"❌ An error occurred: {error}")


@bot.event
async def on_voice_state_update(member, before, after):
    """Auto-disconnect after delay when the bot is alone in voice."""
    # Re-evaluate timer whenever anyone moves/joins/leaves voice in this guild.
    ensure_empty_voice_leave_timer(member.guild)

# --- COMMANDS ---

@bot.command()
async def join(ctx):
    """Joins the user's voice channel."""
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        if ctx.voice_client:
            await ctx.voice_client.move_to(channel)
            await ctx.send(f"🔄 Moved to **{channel}**")
        else:
            started = time.perf_counter()
            await channel.connect(timeout=settings.CONNECTION_TIMEOUT, reconnect=True)
            await asyncio.sleep(settings.CONNECTION_STABILIZE_DELAY)
            elapsed = time.perf_counter() - started
            log_playback_metric(
                "voice_join_ready",
                guild_id=getattr(ctx.guild, 'id', None),
                elapsed_ms=int(elapsed * 1000),
                stabilize_delay_ms=int(settings.CONNECTION_STABILIZE_DELAY * 1000),
            )
            await ctx.send(f"👋 Joined **{channel}**")
    else:
        await ctx.send("❌ You need to be in a voice channel first.")

    if ctx.guild:
        ensure_empty_voice_leave_timer(ctx.guild)

@bot.command()
async def play(ctx, *, query):
    """Plays a LOCAL file from the media folder. Usage: !play <filename>"""
    if not await ensure_voice_connected(ctx):
        return

    # Verify connection
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        return await ctx.send("❌ Failed to connect to voice channel.")

    filename = find_best_match(query)
    if not filename:
        return await ctx.send(f"❌ File not found matching: {query}")

    song_obj = make_song('local', filename, filename, ctx.author)
    song_queue.append(song_obj)

    async with play_next_lock:
        if not ctx.voice_client.is_playing():
            await play_next(ctx)
        else:
            await ctx.send(f"✅ Added to queue: `{filename}` (added by {ctx.author.mention})")

@bot.command()
async def yt(ctx, *, query):
    """Plays from YouTube (URL or search). Usage: !yt <url or search query>"""
    if not await ensure_voice_connected(ctx):
        return

    # Verify connection
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        return await ctx.send("❌ Failed to connect to voice channel.")

    # 1. Determine if the user provided a LINK or a SEARCH PHRASE
    if query.startswith(("http://", "https://", "www.")):
        search_query = query
        await ctx.send(f"🔗 Loading link...")
    else:
        search_query = f"ytsearch:{query}"
        await ctx.send(f"🔎 Searching YouTube for: **{query}**...")

    try:
        yt_query_start = time.perf_counter()
        # Run blocking yt-dlp call in executor to avoid freezing the event loop
        data = await asyncio.to_thread(ytdl.extract_info, search_query, False)
        yt_query_ms = int((time.perf_counter() - yt_query_start) * 1000)
        
        # 3. Handle Search Results vs Direct Links
        if 'entries' in data:
            video_data = data['entries'][0]
        else:
            video_data = data
        
        title = video_data['title']

        if is_blacklisted_title(title):
            return await ctx.send("❌ This song is in the blacklist.")

        # Use webpage_url - this will be processed by yt-dlp again during playback
        webpage_url = video_data.get('webpage_url') or video_data.get('url')
        
        if not webpage_url:
            logger.error(f"No URL found in video data. Keys: {list(video_data.keys())}")
            return await ctx.send("❌ Error: Could not extract URL from video.")
        
        logger.debug(f"Using webpage URL: {webpage_url}")
        log_playback_metric(
            "yt_enqueue_extract",
            query_type=('url' if search_query == query else 'search'),
            extract_ms=yt_query_ms,
            title=(title[:80] if title else None),
        )
        
        song_obj = make_song('youtube', title, webpage_url, ctx.author)
        if video_data.get('url'):
            song_obj['stream_url'] = video_data.get('url')
            song_obj['stream_url_cached_at'] = time.time()
        song_obj['format_id'] = video_data.get('format_id')
        song_obj['ext'] = video_data.get('ext')
        song_obj['duration'] = video_data.get('duration')
        song_obj['uploader'] = video_data.get('uploader') or video_data.get('channel')
        song_obj['thumbnail'] = video_data.get('thumbnail')
        song_obj['webpage_url'] = webpage_url
        song_queue.append(song_obj)
        logger.debug(f"Added song to queue - Title: {title}")

        # Double-check voice connection before playing
        if not ctx.voice_client:
            return await ctx.send("❌ Lost voice connection.")

        async with play_next_lock:
            if not ctx.voice_client.is_playing():
                await play_next(ctx)
            else:
                await ctx.send(f"✅ Added to queue: `{title}` (added by {ctx.author.mention})")

    except Exception as e:
        logger.error(f"Error in yt command: {e}", exc_info=True)
        await ctx.send(f"❌ Error: {e}")

@bot.command()
async def volume(ctx, volume: int):
    """Sets volume (0-100). Usage: !volume <0-100>"""
    global current_volume

    if not await enforce_command_access(ctx, 'volume'):
        return
    
    # Validate input
    if not (0 <= volume <= 100):
        return await ctx.send(f"❌ Volume must be between 0-100. You entered: {volume}")

    # Convert to float (0.0 - 1.0)
    current_volume = volume / 100

    # Adjust currently playing song immediately
    if ctx.voice_client and ctx.voice_client.source:
        ctx.voice_client.source.volume = current_volume

    await ctx.send(f"🔊 Volume set to **{volume}%**")

@bot.command()
async def skip(ctx):
    """Skips current song based on configured permissions and vote rules."""
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("❌ Nothing is playing.")
        return

    mode = get_command_mode('skip')
    vote_cfg = settings.get_skip_vote_config()
    force_vote_for_admin = vote_cfg.get('force_vote_for_admin', False)

    if is_admin_member(ctx.author) and not force_vote_for_admin:
        clear_votes(ctx.guild.id, action_key='skip')
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped by admin.")
        return

    # Let the requester skip their own currently playing song directly.
    if current_song and current_song.get('requester_id') == ctx.author.id:
        clear_votes(ctx.guild.id, action_key='skip')
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped your own song.")
        return

    if mode == 'admin_only':
        await ctx.send("❌ Only administrators can use this command.")
        return

    if mode == 'open':
        clear_votes(ctx.guild.id, action_key='skip')
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped.")
        return

    if vote_cfg['same_channel_only']:
        if not ctx.author.voice or not ctx.voice_client.channel or ctx.author.voice.channel != ctx.voice_client.channel:
            await ctx.send("❌ You must be in the same voice channel as the bot to vote skip.")
            return

    required_votes, eligible_count = get_skip_vote_required_count(ctx)
    votes, already_voted = register_vote(ctx.guild.id, 'skip', ctx.author.id)
    current_votes = len(votes)

    if already_voted:
        await ctx.send(f"🗳️ You already voted to skip. Votes: **{current_votes}/{required_votes}**")
        return

    if current_votes >= required_votes:
        clear_votes(ctx.guild.id, action_key='skip')
        ctx.voice_client.stop()
        await ctx.send(f"⏭️ Vote passed (**{current_votes}/{required_votes}** of {eligible_count} listeners). Skipping.")
        return

    await ctx.send(f"🗳️ Skip vote added (**{current_votes}/{required_votes}** of {eligible_count} listeners).")

@bot.command()
async def queue(ctx):
    """Lists the current queue."""
    if not song_queue:
        await ctx.send("The queue is currently empty.")
        return

    # Build the string
    queue_list = "**Upcoming Songs:**\n"
    for i, song in enumerate(song_queue):
        # i+1 makes it human readable (1, 2, 3 instead of 0, 1, 2)
        queue_list += f"`{i+1}.` {song['title']} - added by {song.get('requester_handle', 'unknown')}\n"

    # Discord has a message limit; if queue is huge, show first N
    max_chars = settings.DISCORD_MESSAGE_CHAR_LIMIT - settings.MESSAGE_BUFFER
    if len(queue_list) > max_chars:
        await ctx.send(f"{queue_list[:max_chars]}...\n*(and more)*")
    else:
        await ctx.send(queue_list)


@bot.command()
async def current(ctx):
    """Shows the currently playing song."""
    if not current_song:
        await ctx.send("❌ No song is currently playing.")
        return

    await ctx.send(
        f"🎶 **Current Song:** {current_song['title']} "
        f"(requested by {current_song.get('requester_mention', 'unknown')})"
    )

@bot.command()
async def skipto(ctx, index: int):
    """Skips to a specific number in the queue. Usage: !skipto <position>"""
    if not await enforce_command_access(ctx, 'skipto'):
        return

    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("❌ Nothing is playing right now.")
        return

    if not song_queue:
        await ctx.send("❌ The queue is empty.")
        return

    # Validate the number
    if index < 1 or index > len(song_queue):
        return await ctx.send(f"❌ Invalid position. Please choose between 1 and {len(song_queue)}.")

    # Logic: Slice the queue to remove everything BEFORE the target
    # index-1 because users see 1-based, list is 0-based
    song_queue[:] = song_queue[index-1:]

    # Stop the current song. This triggers 'play_next', which pulls from our NEW shortened queue.
    ctx.voice_client.stop()
    await ctx.send(f"⏭️ Skipped to position **{index}**.")

@bot.command()
async def clear(ctx):
    """Clears all upcoming songs in the queue."""
    if not await enforce_command_access(ctx, 'clear'):
        return

    song_queue.clear()
    if ctx.guild:
        clear_votes(ctx.guild.id)
    await ctx.send("🗑️ **Queue cleared.**")

@bot.command()
async def stop(ctx):
    """Stops playback and disconnects from voice channel."""
    if not await enforce_command_access(ctx, 'stop'):
        return

    global current_song, current_song_started_perf
    current_song = None
    current_song_started_perf = None
    song_queue.clear()
    if ctx.guild:
        cancel_empty_voice_leave_timer(ctx.guild.id)
        clear_votes(ctx.guild.id)
        cancel_playback_monitor(ctx.guild.id)
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await clear_bot_status()
        await ctx.send("🛑 Stopped and disconnected.")
    else:
        await ctx.send("❌ Not connected to a voice channel.")


@bot.command()
async def remove(ctx, index: int):
    """Removes a song from queue by index. Owner/admin can remove directly; others require vote."""
    if not song_queue:
        await ctx.send("❌ The queue is empty.")
        return

    if index < 1 or index > len(song_queue):
        await ctx.send(f"❌ Invalid position. Please choose between 1 and {len(song_queue)}.")
        return

    target = song_queue[index - 1]
    vote_cfg = settings.get_skip_vote_config()
    force_vote_for_admin = vote_cfg.get('force_vote_for_admin', False)
    is_admin = is_admin_member(ctx.author)
    is_owner = target.get('requester_id') == ctx.author.id

    if (is_admin and not force_vote_for_admin) or is_owner:
        removed_song = song_queue.pop(index - 1)
        if ctx.guild:
            clear_votes(ctx.guild.id, action_key=f"remove:{removed_song['queue_id']}")
        await ctx.send(f"🗑️ Removed `#{index}`: **{removed_song['title']}**")
        return

    if vote_cfg['same_channel_only']:
        if not ctx.voice_client or not ctx.author.voice or not ctx.voice_client.channel or ctx.author.voice.channel != ctx.voice_client.channel:
            await ctx.send("❌ You must be in the same voice channel as the bot to vote-remove this song.")
            return

    required_votes, eligible_count = get_skip_vote_required_count(ctx)
    action_key = f"remove:{target['queue_id']}"
    votes, already_voted = register_vote(ctx.guild.id, action_key, ctx.author.id)
    current_votes = len(votes)

    if already_voted:
        await ctx.send(f"🗳️ You already voted to remove `#{index}`. Votes: **{current_votes}/{required_votes}**")
        return

    if current_votes >= required_votes:
        current_index = next((i for i, s in enumerate(song_queue) if s.get('queue_id') == target['queue_id']), None)
        if current_index is None:
            clear_votes(ctx.guild.id, action_key=action_key)
            await ctx.send("ℹ️ That song is no longer in the queue.")
            return

        removed_song = song_queue.pop(current_index)
        clear_votes(ctx.guild.id, action_key=action_key)
        await ctx.send(
            f"🗑️ Vote passed (**{current_votes}/{required_votes}** of {eligible_count} listeners). "
            f"Removed **{removed_song['title']}**."
        )
        return

    await ctx.send(
        f"🗳️ Remove vote added for `#{index}` (**{current_votes}/{required_votes}** of {eligible_count} listeners)."
    )


@bot.command()
async def block(ctx, user_id: int = None):
    """Adds a user to the blocked list. Usage: !block <user_id>"""
    if not await enforce_command_access(ctx, 'block'):
        return

    if user_id is None:
        blocked_ids = settings.get_blocked_user_ids()
        if not blocked_ids:
            return await ctx.send("📝 No users are currently blocked.")
        return await ctx.send(f"📝 **Blocked User IDs:** {', '.join(map(str, blocked_ids))}")

    blocked_ids = settings.get_blocked_user_ids()
    if user_id in blocked_ids:
        await ctx.send(f"ℹ️ User `{user_id}` is already blocked.")
        return

    blocked_ids.add(user_id)
    if settings.update_blocked_user_ids(blocked_ids):
        await ctx.send(f"✅ User `{user_id}` has been **blocked**.")
    else:
        await ctx.send("❌ Failed to update configuration.")


@bot.command()
async def unblock(ctx, user_id: int = None):
    """Removes a user from the blocked list. Usage: !unblock <user_id>"""
    if not await enforce_command_access(ctx, 'unblock'):
        return

    if user_id is None:
        blocked_ids = settings.get_blocked_user_ids()
        if not blocked_ids:
            return await ctx.send("📝 No users are currently blocked.")
        return await ctx.send(f"📝 **Blocked User IDs:** {', '.join(map(str, blocked_ids))}")

    blocked_ids = settings.get_blocked_user_ids()
    if user_id not in blocked_ids:
        await ctx.send(f"ℹ️ User `{user_id}` is not currently blocked.")
        return

    blocked_ids.remove(user_id)
    if settings.update_blocked_user_ids(blocked_ids):
        await ctx.send(f"✅ User `{user_id}` has been **unblocked**.")
    else:
        await ctx.send("❌ Failed to update configuration.")


@bot.command()
async def whitelist(ctx, user_id: int = None):
    """Lists whitelist status or adds a user to whitelist. Usage: !whitelist [user_id]"""
    if not await enforce_command_access(ctx, 'whitelist'):
        return

    allowed_ids = settings.get_allowed_user_ids()

    if user_id is None:
        if not allowed_ids:
            return await ctx.send("📝 Whitelist is disabled (mode: everyone except blocked users).")
        return await ctx.send(f"📝 **Whitelisted User IDs:** {', '.join(map(str, sorted(allowed_ids)))}")

    if allowed_ids is None:
        allowed_ids = set()

    if user_id in allowed_ids:
        await ctx.send(f"ℹ️ User `{user_id}` is already whitelisted.")
        return

    allowed_ids.add(user_id)
    if settings.update_allowed_user_ids(sorted(allowed_ids)):
        await ctx.send(f"✅ User `{user_id}` has been **whitelisted**.")
    else:
        await ctx.send("❌ Failed to update configuration.")


@bot.command()
async def unwhitelist(ctx, user_id: int = None):
    """Removes a user from whitelist or disables whitelist mode. Usage: !unwhitelist [user_id]"""
    if not await enforce_command_access(ctx, 'unwhitelist'):
        return

    allowed_ids = settings.get_allowed_user_ids()
    if not allowed_ids:
        return await ctx.send("📝 Whitelist is already disabled.")

    if user_id is None:
        if settings.update_allowed_user_ids(None):
            await ctx.send("✅ Whitelist disabled. Access mode is now: everyone except blocked users.")
        else:
            await ctx.send("❌ Failed to update configuration.")
        return

    if user_id not in allowed_ids:
        await ctx.send(f"ℹ️ User `{user_id}` is not currently whitelisted.")
        return

    allowed_ids.remove(user_id)
    next_value = sorted(allowed_ids) if allowed_ids else None
    if settings.update_allowed_user_ids(next_value):
        if next_value is None:
            await ctx.send(
                f"✅ User `{user_id}` removed. Whitelist is now empty, so whitelist mode was disabled."
            )
        else:
            await ctx.send(f"✅ User `{user_id}` has been **removed from whitelist**.")
    else:
        await ctx.send("❌ Failed to update configuration.")


@bot.command()
async def blacklist(ctx, *, pattern: str = None):
    """Lists or adds/removes patterns from the YouTube title blacklist. Usage: !blacklist [pattern]"""
    if not await enforce_command_access(ctx, 'blacklist'):
        return

    global _blacklist_patterns
    patterns = settings.get_blacklist_patterns()

    if pattern is None:
        if not patterns:
            return await ctx.send("📝 The YouTube title blacklist is empty.")
        
        patterns_text = "**Blacklist Patterns:**\n"
        for i, p in enumerate(patterns):
            patterns_text += f"`{i+1}.` `{p}`\n"
        
        if len(patterns_text) > 1900:
            await ctx.send(f"{patterns_text[:1900]}...\n*(and more)*")
        else:
            await ctx.send(patterns_text)
        return

    if pattern in patterns:
        patterns.remove(pattern)
        if settings.update_blacklist_patterns(patterns):
            _blacklist_patterns = load_yt_blacklist_patterns()
            await ctx.send(f"✅ Removed pattern from blacklist: `{pattern}`")
        else:
            await ctx.send("❌ Failed to update configuration.")
    else:
        # Basic validation of regex
        try:
            re.compile(pattern)
        except re.error:
            return await ctx.send(f"❌ Invalid regex pattern: `{pattern}`")

        patterns.append(pattern)
        if settings.update_blacklist_patterns(patterns):
            _blacklist_patterns = load_yt_blacklist_patterns()
            await ctx.send(f"✅ Added pattern to blacklist: `{pattern}`")
        else:
            await ctx.send("❌ Failed to update configuration.")


@bot.command()
@commands.is_owner()
async def reload_blacklist(ctx):
    """Reloads the YouTube blacklist patterns from config.yaml. Owner only.
    
    Note: After editing config.yaml, run this command to reload patterns without restarting the bot.
    """
    global _blacklist_patterns
    try:
        # Reload config module to pick up changes from config.yaml
        importlib.reload(settings)
        _blacklist_patterns = load_yt_blacklist_patterns()
        count = len(_blacklist_patterns)
        await ctx.send(f"✅ Blacklist reloaded from config.yaml. {count} patterns loaded.")
        logger.info(f"Blacklist reloaded from config.yaml with {count} patterns.")
    except Exception as e:
        logger.error(f"Failed to reload blacklist: {e}", exc_info=True)
        await ctx.send(f"❌ Failed to reload blacklist: {e}")

# Initialize blacklist patterns on startup
@bot.event
async def setup_hook():
    """Called after the bot is logged in but before on_ready."""
    global _blacklist_patterns
    try:
        validate_command_permissions_config()
        _blacklist_patterns = load_yt_blacklist_patterns()
        ensure_loop_lag_monitor()
        await start_web_api()
        logger.info(f"Blacklist initialized with {len(_blacklist_patterns)} patterns.")
    except Exception as e:
        logger.error(f"Failed to initialize blacklist: {e}", exc_info=True)

# Main bot startup
if __name__ == '__main__':
    logger.info("Starting MusicBot...")
    bot.run(TOKEN)