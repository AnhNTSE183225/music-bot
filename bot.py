import discord
from discord.ext import commands
import os
import sys
import io
import asyncio
import threading
import yt_dlp
import difflib
import re
from dotenv import load_dotenv
import logging
import importlib
import math
import time
import shlex
from datetime import datetime, timedelta, timezone
from yt_query_logic import is_probable_url, is_youtube_link, normalize_yt_search_term

load_dotenv()

import settings

# Configure logging with UTF-8 encoding to handle emoji and Unicode characters
# Create a UTF-8 stream wrapper for stdout to handle emoji
class UTF8StreamHandler(logging.StreamHandler):
    def __init__(self):
        # Use a UTF-8 text wrapper around stdout.buffer
        utf8_stream = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        super().__init__(utf8_stream)

# Configure logging with custom UTF-8 handler
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[UTF8StreamHandler()]
)
logger = logging.getLogger(__name__)

# Configure file handler with UTF-8 encoding to handle emoji and Unicode
file_handler = logging.FileHandler(settings.LOG_FILE, encoding='utf-8')
file_handler.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

TOKEN = os.getenv(settings.TOKEN_ENV_VAR)

# Validate token on startup
if not TOKEN:
    TOKEN = None

# --- SETUP ---
import subprocess
import threading

class LoggingFFmpegPCMAudio(discord.FFmpegPCMAudio):
    def __init__(self, *args, **kwargs):
        # We need to capture stderr. FFmpegPCMAudio uses subprocess.Popen.
        # So we pass stderr=subprocess.PIPE to it.
        kwargs['stderr'] = subprocess.PIPE
        super().__init__(*args, **kwargs)
        
        # Now self._process is the Popen object
        self._stderr_thread = threading.Thread(target=self._log_stderr)
        self._stderr_thread.daemon = True
        self._stderr_thread.start()

    def _log_stderr(self):
        if hasattr(self, '_process') and self._process and self._process.stderr:
            for line in iter(self._process.stderr.readline, b''):
                if not line:
                    break
                try:
                    logger.warning(f"[ffmpeg] {line.decode('utf-8', 'ignore').strip()}")
                except Exception:
                    pass

intents = discord.Intents.all()

bot = commands.Bot(command_prefix=settings.COMMAND_PREFIX, intents=intents)

if settings.CONSOLE_USER_ID is not None:
    bot.owner_id = settings.CONSOLE_USER_ID

ytdl = yt_dlp.YoutubeDL(settings.YTDL_OPTIONS)
search_ytdl_options = dict(settings.YTDL_OPTIONS)
search_ytdl_options['ignoreerrors'] = True
search_ytdl = yt_dlp.YoutubeDL(search_ytdl_options)

# GLOBAL VARIABLES
music_state_by_guild = {}
play_next_locks_by_guild = {}
votes_by_guild = {}
next_queue_id = 1
empty_voice_leave_tasks = {}
EMPTY_VOICE_LEAVE_DELAY_SECONDS = 10
playback_monitor_tasks = {}
loop_lag_monitor_task = None
console_command_queue = None
console_input_thread = None
console_input_thread_stop = None
console_command_consumer_task = None
console_command_bridge_started = False

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


def get_music_state(guild):
    """Return the mutable playback state for one guild."""
    guild_id = getattr(guild, 'id', None)
    if guild_id is None:
        return None

    state = music_state_by_guild.get(guild_id)
    if state is None:
        state = {
            'queue': [],
            'queue_index': -1,
            'loop_enabled': False,
            'current_song': None,
            'volume': settings.DEFAULT_VOLUME,
        }
        music_state_by_guild[guild_id] = state
    return state


def get_music_queue(guild):
    state = get_music_state(guild)
    return state['queue'] if state else []


def get_current_song(guild):
    state = get_music_state(guild)
    return state['current_song'] if state else None


def set_current_song(guild, song):
    state = get_music_state(guild)
    if state is not None:
        state['current_song'] = song


def get_music_volume(guild):
    state = get_music_state(guild)
    return state['volume'] if state else settings.DEFAULT_VOLUME


def set_music_volume(guild, volume):
    state = get_music_state(guild)
    if state is not None:
        state['volume'] = volume


def clear_music_state(guild):
    state = get_music_state(guild)
    if state is not None:
        state['queue'].clear()
        state['queue_index'] = -1
        state['current_song'] = None


def any_music_playing():
    return any(state.get('current_song') for state in music_state_by_guild.values())


async def clear_bot_status_if_idle():
    if not any_music_playing():
        await clear_bot_status()


def get_play_next_lock(guild_id):
    lock = play_next_locks_by_guild.get(guild_id)
    if lock is None:
        lock = asyncio.Lock()
        play_next_locks_by_guild[guild_id] = lock
    return lock


class ConsoleChannel:
    """Minimal channel adapter that prints command responses to stdout."""

    def __init__(self, label="console"):
        self.label = label

    async def send(self, content=None, **kwargs):
        if content is None:
            content = kwargs.get('content')
        if content is None and 'embed' in kwargs:
            content = str(kwargs['embed'])
        if content is not None:
            print(f"[{self.label}] {content}")
        return None


class ConsoleAuthor:
    """Proxy author that routes console commands through a fixed Discord identity."""

    def __init__(self, user_id, display_name, voice=None):
        self.id = user_id
        self.name = display_name
        self.display_name = display_name
        self.mention = f"<@{user_id}>"
        self.bot = False
        self.voice = voice
        self.guild_permissions = type("ConsoleGuildPermissions", (), {"administrator": True})()

    def __str__(self):
        return self.display_name


def create_console_author(member=None, voice=None):
    """Build a proxy author for console commands."""
    display_name = "Console User"
    if member is not None:
        display_name = getattr(member, 'display_name', None) or getattr(member, 'name', None) or display_name

    effective_voice = getattr(member, 'voice', None) or voice
    return ConsoleAuthor(settings.CONSOLE_USER_ID, display_name, effective_voice)


async def resolve_console_target():
    """Find the guild and member context to emulate for console commands."""
    if settings.CONSOLE_USER_ID is None:
        return None, None

    for guild in bot.guilds:
        member = getattr(guild, 'get_member', lambda _user_id: None)(settings.CONSOLE_USER_ID)
        if member is None and hasattr(guild, 'fetch_member'):
            try:
                member = await guild.fetch_member(settings.CONSOLE_USER_ID)
            except Exception:
                member = None

        if member and getattr(member, 'voice', None) and member.voice.channel:
            return guild, create_console_author(member)

    for guild in bot.guilds:
        voice_client = getattr(guild, 'voice_client', None)
        if voice_client and voice_client.is_connected():
            member = getattr(guild, 'get_member', lambda _user_id: None)(settings.CONSOLE_USER_ID)
            if member is None and hasattr(guild, 'fetch_member'):
                try:
                    member = await guild.fetch_member(settings.CONSOLE_USER_ID)
                except Exception:
                    member = None

            fallback_voice = type("ConsoleVoiceState", (), {"channel": voice_client.channel})()
            return guild, create_console_author(member, fallback_voice)

    if bot.guilds:
        guild = bot.guilds[0]
        member = getattr(guild, 'get_member', lambda _user_id: None)(settings.CONSOLE_USER_ID)
        if member is None and hasattr(guild, 'fetch_member'):
            try:
                member = await guild.fetch_member(settings.CONSOLE_USER_ID)
            except Exception:
                member = None

        return guild, create_console_author(member)

    return None, None


async def dispatch_console_command(raw_line):
    """Run one console line through the same command parser as Discord messages."""
    if not raw_line or not raw_line.strip():
        return

    prefix = settings.COMMAND_PREFIX
    if not raw_line.startswith(prefix):
        logger.info("Ignoring console input without command prefix: %s", raw_line)
        return

    command_line = raw_line[len(prefix):].strip()
    if not command_line:
        return

    try:
        parts = shlex.split(command_line)
    except ValueError as exc:
        logger.warning("Invalid console command syntax: %s (%s)", raw_line, exc)
        return

    if not parts:
        return

    command_name = parts[0].lower()
    command = bot.get_command(command_name)
    if command is None:
        logger.warning("Unknown console command: %s", raw_line)
        return

    guild, author = await resolve_console_target()
    if author is None:
        logger.warning("Console commands are disabled until USER_ID is configured.")
        return

    # Log guild name and voice safely (avoid Unicode encoding issues on Windows console)
    guild_name = getattr(guild, 'name', 'unknown')
    voice_channel = getattr(getattr(author, 'voice', None), 'channel', None)
    try:
        logger.info(
            "Console command received: %s (guild=%s, voice=%s)",
            raw_line,
            str(guild_name),
            str(voice_channel),
        )
    except UnicodeEncodeError:
        # Fallback for console encoding issues
        logger.info("Console command received: %s", raw_line)

    message = type("ConsoleMessage", (), {})()
    message.content = raw_line
    message.author = author
    message.guild = guild
    message.channel = ConsoleChannel(guild.name if guild else "console")
    message.attachments = []
    message.reference = None
    message.id = int(time.time() * 1000)

    class ConsoleContext:
        @property
        def voice_client(self):
            return getattr(self.guild, 'voice_client', None) if getattr(self, 'guild', None) else None

    ctx = ConsoleContext()
    ctx.bot = bot
    ctx.message = message
    ctx.guild = guild
    ctx.author = author
    ctx.channel = message.channel
    ctx.command = command
    ctx.invoked_with = command_name
    ctx.prefix = prefix
    ctx.args = []
    ctx.kwargs = {}

    async def send(content=None, **kwargs):
        return await message.channel.send(content, **kwargs)

    ctx.send = send

    async def run_command():
        if command_name in {'play', 'yt', 'blacklist'}:
            query = command_line[len(command_name):].strip()
            if command_name == 'blacklist':
                await command.callback(ctx, pattern=(query or None))
            else:
                if not query:
                    await ctx.send(f"❌ Usage: {prefix}{command_name} <query>")
                    return
                await command.callback(ctx, query=query)
            return

        if command_name in {'volume', 'skipto', 'remove'}:
            if len(parts) < 2:
                await ctx.send(f"❌ Usage: {prefix}{command_name} <number>")
                return
            try:
                value = int(parts[1])
            except ValueError:
                await ctx.send(f"❌ `{parts[1]}` is not a valid number.")
                return
            await command.callback(ctx, value)
            return

        if command_name in {'block', 'unblock', 'whitelist', 'unwhitelist'}:
            if len(parts) < 2:
                await command.callback(ctx, None)
                return
            try:
                user_id = int(parts[1])
            except ValueError:
                await ctx.send(f"❌ `{parts[1]}` is not a valid user ID.")
                return
            await command.callback(ctx, user_id)
            return

        await command.callback(ctx)

    try:
        await run_command()
    except Exception as exc:
        logger.error("Console command failed: %s", raw_line, exc_info=True)
        await ctx.send(f"❌ Console command failed: {exc}")


async def consume_console_commands():
    """Consume console input lines sequentially on the bot event loop."""
    while True:
        raw_line = await console_command_queue.get()
        if raw_line is None:
            return
        await dispatch_console_command(raw_line)


def start_console_command_bridge():
    """Start the stdin reader thread and the async consumer once."""
    global console_command_queue, console_input_thread, console_input_thread_stop
    global console_command_consumer_task, console_command_bridge_started

    if console_command_bridge_started:
        return

    if settings.CONSOLE_USER_ID is None:
        logger.info("Console command bridge disabled. Set USER_ID in .env to enable it.")
        return

    console_command_bridge_started = True
    console_command_queue = asyncio.Queue()
    console_input_thread_stop = threading.Event()
    console_command_consumer_task = bot.loop.create_task(consume_console_commands())

    def reader():
        try:
            while not console_input_thread_stop.is_set():
                try:
                    line = sys.stdin.readline()
                    if line == "":
                        break

                    raw_line = line.rstrip("\r\n")
                    if not raw_line.strip():
                        continue

                    try:
                        asyncio.run_coroutine_threadsafe(console_command_queue.put(raw_line), bot.loop)
                    except RuntimeError:
                        break
                except (EOFError, ValueError):
                    # stdin closed or other readline error
                    break
        except KeyboardInterrupt:
            # Handle Ctrl+C in reader thread
            pass
        finally:
            # Signal the stop event to ensure clean shutdown
            console_input_thread_stop.set()

    console_input_thread = threading.Thread(target=reader, name="MusicBotConsoleInput", daemon=True)
    console_input_thread.start()


def stop_console_command_bridge():
    """Stop the console bridge during shutdown."""
    global console_input_thread_stop, console_command_consumer_task

    if console_input_thread_stop is not None:
        console_input_thread_stop.set()

    if console_command_queue is not None:
        try:
            console_command_queue.put_nowait(None)
        except Exception:
            pass

    if console_command_consumer_task and not console_command_consumer_task.done():
        console_command_consumer_task.cancel()


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

                active_song = get_current_song(ctx.guild)
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


async def get_playable_search_result(search_term, max_results=10):
    """Resolve a search term to the first playable YouTube result.

    Uses ignoreerrors for search listing so restricted/unavailable results are skipped,
    then validates each candidate with the main yt-dlp options.
    """
    search_expr = f"ytsearch{max_results}:{search_term}"
    data = await asyncio.to_thread(search_ytdl.extract_info, search_expr, False)
    entries = []
    if data and isinstance(data, dict):
        entries = data.get('entries') or []

    skip_count = 0
    last_error = None

    for entry in entries:
        if not entry:
            skip_count += 1
            continue

        title = entry.get('title') or "Unknown title"
        if is_blacklisted_title(title):
            logger.info("Skipping blacklisted YouTube title from search: %s", title)
            skip_count += 1
            continue

        candidate_url = entry.get('webpage_url') or entry.get('url')
        if not candidate_url:
            skip_count += 1
            continue

        try:
            candidate_data = await asyncio.to_thread(ytdl.extract_info, candidate_url, False)
        except Exception as exc:
            # Restricted, age-gated, private, or unavailable result. Try next.
            last_error = exc
            skip_count += 1
            logger.warning("Skipping unavailable/restricted YouTube result '%s': %s", title, exc)
            continue

        if candidate_data and isinstance(candidate_data, dict) and 'entries' in candidate_data:
            nested_entries = candidate_data.get('entries') or []
            candidate_data = next((item for item in nested_entries if item), None)

        if not candidate_data:
            skip_count += 1
            continue

        stream_url = candidate_data.get('url')
        webpage_url = candidate_data.get('webpage_url') or candidate_url
        if not stream_url and not webpage_url:
            skip_count += 1
            continue

        return candidate_data, skip_count

    if last_error:
        raise last_error
    raise ValueError("No playable YouTube results found")


async def ensure_voice_connected(ctx):
    """Ensure bot is connected to user's voice channel. Returns True on success."""
    if ctx.voice_client and ctx.voice_client.is_connected():
        return True
    
    if not ctx.author.voice:
        await ctx.send("❌ You must be in a voice channel first.")
        return False
    
    try:
        started = time.perf_counter()
        voice_client = await ctx.author.voice.channel.connect(timeout=settings.CONNECTION_TIMEOUT, reconnect=True)
        if ctx.guild is not None:
            try:
                ctx.guild.voice_client = voice_client
            except (AttributeError, TypeError):
                pass
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
        try:
            await asyncio.sleep(EMPTY_VOICE_LEAVE_DELAY_SECONDS)
            fresh_voice_client = guild.voice_client
            if not fresh_voice_client or not fresh_voice_client.is_connected():
                return
            if get_non_bot_voice_member_count(fresh_voice_client) > 0:
                return
            if fresh_voice_client.is_playing() or fresh_voice_client.is_paused():
                return

            clear_music_state(guild)
            clear_votes(guild_id)
            fresh_voice_client.stop()
            try:
                await asyncio.wait_for(fresh_voice_client.disconnect(force=True), timeout=5.0)
            except Exception as e:
                logger.warning("Voice disconnect timed out/failed: %s", e)
            await clear_bot_status_if_idle()
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
    music_state = get_music_state(ctx.guild)
    if music_state is None:
        return

    queue = music_state['queue']

    # Check if voice client exists and is connected
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        logger.debug("Voice client not connected, cannot play next song")
        return

    # Guard against concurrent starters.
    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        return

    if not queue:
        music_state['current_song'] = None
        music_state['queue_index'] = -1
        await clear_bot_status_if_idle()
        return

    if 'next_index' in music_state and music_state['next_index'] is not None:
        next_idx = music_state['next_index']
        music_state['next_index'] = None
    else:
        next_idx = music_state.get('queue_index', -1) + 1

    if next_idx >= len(queue):
        if music_state.get('loop_enabled', False):
            next_idx = 0
        else:
            music_state['current_song'] = None
            await clear_bot_status_if_idle()
            return

    if next_idx < 0:
        next_idx = 0

    music_state['queue_index'] = next_idx
    song = queue[next_idx]
    music_state['current_song'] = song  # Track currently playing song
    queue_wait_ms = int((time.perf_counter() - song.get('enqueued_perf', time.perf_counter())) * 1000)

    try:
        logger.debug(f"Now playing - {song['type']}: {song['title']}")
        # 1. Create the base Source
        if song['type'] == 'local':
            source_path = os.path.join(settings.MEDIA_FOLDER, song['data'])
            source_init_start = time.perf_counter()
            source = LoggingFFmpegPCMAudio(source_path)
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
            source = LoggingFFmpegPCMAudio(filename, **settings.FFMPEG_OPTIONS)
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
            source = LoggingFFmpegPCMAudio(
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
        source.volume = get_music_volume(ctx.guild)

        # 3. Play - double check connection and playback state before playing
        if ctx.voice_client and ctx.voice_client.is_connected():
            if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
                # Another call started playback while we were preparing this source.
                queue.insert(0, song)
                return

            playback_started = time.perf_counter()
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
                    async with get_play_next_lock(getattr(ctx.guild, 'id', None)):
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
                queue_size_after_pop=len(queue),
                guild_id=getattr(ctx.guild, 'id', None),
            )
            
            # Update bot's status to show currently playing song
            await update_bot_status(song['title'])
            
            await ctx.send(
                f"🎶 **Now Playing:** {song['title']} "
                f"(requested by {song.get('requester_mention', 'unknown')}, Vol: {int(get_music_volume(ctx.guild) * 100)}%)"
            )
        else:
            logger.warning("Lost connection before playing")
            queue.insert(0, song)  # Put song back in queue
            await ctx.send("❌ Lost voice connection")

    except Exception as e:
        logger.error(f"Error in play_next: {e}", exc_info=True)
        await ctx.send(f"❌ Error playing: {e}")
        # Try next song after a small delay
        await asyncio.sleep(1)
        if ctx.voice_client and ctx.voice_client.is_connected():
            async with get_play_next_lock(getattr(ctx.guild, 'id', None)):
                await play_next(ctx)

# --- EVENTS ---

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user}')
    start_console_command_bridge()

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
            voice_client = await channel.connect(timeout=settings.CONNECTION_TIMEOUT, reconnect=True)
            if ctx.guild is not None:
                try:
                    ctx.guild.voice_client = voice_client
                except (AttributeError, TypeError):
                    pass
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
    queue = get_music_queue(ctx.guild)
    queue.append(song_obj)

    async with get_play_next_lock(getattr(ctx.guild, 'id', None)):
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

    # 1. Determine if input is direct URL (YouTube or generic) or a search term.
    #    Lyrics normalization is only for search terms.
    if is_youtube_link(query):
        search_query = query
        await ctx.send(f"🔗 Loading link...")
        query_type = 'url'
    elif is_probable_url(query):
        search_query = query
        await ctx.send(f"🔗 Loading link...")
        query_type = 'url'
    else:
        effective_search_term, _ = normalize_yt_search_term(query)
        if is_blacklisted_title(query) or is_blacklisted_title(effective_search_term):
            return await ctx.send("❌ This song is in the blacklist.")
        await ctx.send(f"🔎 Searching YouTube for: **{query}**...")
        query_type = 'search'

    try:
        yt_query_start = time.perf_counter()
        # Run blocking yt-dlp calls in a worker thread to avoid freezing the event loop.
        if query_type == 'url':
            data = await asyncio.to_thread(ytdl.extract_info, search_query, False)
            skipped_results = 0
            if data and isinstance(data, dict) and 'entries' in data:
                entries = data.get('entries') or []
                video_data = next((item for item in entries if item), None)
            else:
                video_data = data
        else:
            video_data, skipped_results = await get_playable_search_result(effective_search_term, max_results=10)

        yt_query_ms = int((time.perf_counter() - yt_query_start) * 1000)

        if not video_data:
            return await ctx.send("❌ Error: Could not find a playable YouTube result.")
        
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
            query_type=query_type,
            extract_ms=yt_query_ms,
            skipped_results=skipped_results,
            title=(title[:80] if title else None),
        )

        if query_type == 'search' and skipped_results > 0:
            await ctx.send(f"ℹ️ Skipped **{skipped_results}** unavailable/restricted result(s) before finding a playable match.")
        
        song_obj = make_song('youtube', title, webpage_url, ctx.author)
        if video_data.get('url'):
            song_obj['stream_url'] = video_data.get('url')
            song_obj['stream_url_cached_at'] = time.time()
        song_obj['format_id'] = video_data.get('format_id')
        song_obj['ext'] = video_data.get('ext')
        song_obj['duration'] = video_data.get('duration')
        queue = get_music_queue(ctx.guild)
        queue.append(song_obj)
        logger.debug(f"Added song to queue - Title: {title}")

        # Double-check voice connection before playing
        if not ctx.voice_client:
            return await ctx.send("❌ Lost voice connection.")

        async with get_play_next_lock(getattr(ctx.guild, 'id', None)):
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
    if not await enforce_command_access(ctx, 'volume'):
        return
    
    # Validate input
    if not (0 <= volume <= 100):
        return await ctx.send(f"❌ Volume must be between 0-100. You entered: {volume}")

    # Convert to float (0.0 - 1.0)
    music_volume = volume / 100
    set_music_volume(ctx.guild, music_volume)

    # Adjust currently playing song immediately
    if ctx.voice_client and ctx.voice_client.source:
        ctx.voice_client.source.volume = music_volume

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
    current_song = get_current_song(ctx.guild)
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
    queue = get_music_queue(ctx.guild)
    if not queue:
        await ctx.send("The queue is currently empty.")
        return

    music_state = get_music_state(ctx.guild)
    curr_idx = music_state.get('queue_index', -1) if music_state else -1

    # Build the string
    queue_list = "**Playlist:**\n"
    for i, song in enumerate(queue):
        # i+1 makes it human readable (1, 2, 3 instead of 0, 1, 2)
        marker = "▶️ " if i == curr_idx else ""
        queue_list += f"`{i+1}.` {marker}{song['title']} - added by {song.get('requester_handle', 'unknown')}\n"

    # Discord has a message limit; if queue is huge, show first N
    max_chars = settings.DISCORD_MESSAGE_CHAR_LIMIT - settings.MESSAGE_BUFFER
    if len(queue_list) > max_chars:
        await ctx.send(f"{queue_list[:max_chars]}...\n*(and more)*")
    else:
        await ctx.send(queue_list)


@bot.command()
async def current(ctx):
    """Shows the currently playing song."""
    current_song = get_current_song(ctx.guild)
    if not current_song:
        await ctx.send("❌ No song is currently playing.")
        return

    await ctx.send(
        f"🎶 **Current Song:** {current_song['title']} "
        f"(requested by {current_song.get('requester_mention', 'unknown')})"
    )

@bot.command()
async def loop(ctx):
    """Toggles whether the playlist loops when reaching the end. Usage: !loop"""
    if not await enforce_command_access(ctx, 'loop'):
        return

    music_state = get_music_state(ctx.guild)
    if not music_state:
        return
        
    current = music_state.get('loop_enabled', False)
    music_state['loop_enabled'] = not current
    
    status = "enabled" if music_state['loop_enabled'] else "disabled"
    await ctx.send(f"🔁 Playlist looping is now **{status}**.")

@bot.command()
async def skipto(ctx, index: int):
    """Skips to a specific number in the queue. Usage: !skipto <position>"""
    if not await enforce_command_access(ctx, 'skipto'):
        return

    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("❌ Nothing is playing right now.")
        return

    queue = get_music_queue(ctx.guild)
    if not queue:
        await ctx.send("❌ The queue is empty.")
        return

    # Validate the number
    if index < 1 or index > len(queue):
        return await ctx.send(f"❌ Invalid position. Please choose between 1 and {len(queue)}.")

    # Set the next_index in the music state and stop playback
    # index-1 because users see 1-based, list is 0-based
    music_state = get_music_state(ctx.guild)
    if music_state:
        music_state['next_index'] = index - 1

    # Stop the current song. This triggers 'play_next', which pulls the song at next_index.
    ctx.voice_client.stop()
    await ctx.send(f"⏭️ Skipped to position **{index}**.")

@bot.command()
async def clear(ctx):
    """Clears all upcoming songs in the queue."""
    if not await enforce_command_access(ctx, 'clear'):
        return

    get_music_queue(ctx.guild).clear()
    music_state = get_music_state(ctx.guild)
    if music_state:
        music_state['queue_index'] = -1
        music_state['current_song'] = None
        
    if ctx.voice_client:
        ctx.voice_client.stop()

    if ctx.guild:
        clear_votes(ctx.guild.id)
    await ctx.send("🗑️ **Playlist cleared.**")

@bot.command()
async def stop(ctx):
    """Stops playback and disconnects from voice channel."""
    if not await enforce_command_access(ctx, 'stop'):
        return

    clear_music_state(ctx.guild)
    if ctx.guild:
        cancel_empty_voice_leave_timer(ctx.guild.id)
        clear_votes(ctx.guild.id)
        cancel_playback_monitor(ctx.guild.id)
    if ctx.voice_client:
        ctx.voice_client.stop()
        try:
            await asyncio.wait_for(ctx.voice_client.disconnect(force=True), timeout=5.0)
        except Exception as e:
            logger.warning("Stop disconnect timed out/failed: %s", e)
        await clear_bot_status_if_idle()
        await ctx.send("🛑 Stopped and disconnected.")
    else:
        await ctx.send("❌ Not connected to a voice channel.")


@bot.command()
async def remove(ctx, index: int):
    """Removes a song from queue by index. Owner/admin can remove directly; others require vote."""
    queue = get_music_queue(ctx.guild)
    if not queue:
        await ctx.send("❌ The queue is empty.")
        return

    if index < 1 or index > len(queue):
        await ctx.send(f"❌ Invalid position. Please choose between 1 and {len(queue)}.")
        return

    target = queue[index - 1]
    vote_cfg = settings.get_skip_vote_config()
    force_vote_for_admin = vote_cfg.get('force_vote_for_admin', False)
    is_admin = is_admin_member(ctx.author)
    is_owner = target.get('requester_id') == ctx.author.id

    if (is_admin and not force_vote_for_admin) or is_owner:
        removed_song = queue.pop(index - 1)
        music_state = get_music_state(ctx.guild)
        if music_state:
            curr = music_state.get('queue_index', -1)
            if index - 1 < curr:
                music_state['queue_index'] -= 1
            elif index - 1 == curr:
                music_state['next_index'] = index - 1
                if ctx.voice_client and ctx.voice_client.is_playing():
                    ctx.voice_client.stop()
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
        current_index = next((i for i, s in enumerate(queue) if s.get('queue_id') == target['queue_id']), None)
        if current_index is None:
            clear_votes(ctx.guild.id, action_key=action_key)
            await ctx.send("ℹ️ That song is no longer in the queue.")
            return

        removed_song = queue.pop(current_index)
        music_state = get_music_state(ctx.guild)
        if music_state:
            curr = music_state.get('queue_index', -1)
            if current_index < curr:
                music_state['queue_index'] -= 1
            elif current_index == curr:
                music_state['next_index'] = current_index
                if ctx.voice_client and ctx.voice_client.is_playing():
                    ctx.voice_client.stop()
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
        logger.info(f"Blacklist initialized with {len(_blacklist_patterns)} patterns.")
    except Exception as e:
        logger.error(f"Failed to initialize blacklist: {e}", exc_info=True)

# Main bot startup
async def main():
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable not set. "
            "Please add it to your .env file or system environment."
        )

    logger.info("Starting MusicBot...")
    try:
        await bot.start(TOKEN)
    except KeyboardInterrupt:
        logger.info("Received Ctrl+C, shutting down gracefully...")
        await bot.close()
    finally:
        stop_console_command_bridge()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("MusicBot terminated by user.")
        print("\nMusicBot terminated by user.")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        print(f"\nFatal error: {e}")
        raise