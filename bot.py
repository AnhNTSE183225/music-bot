import discord
from discord.ext import commands
import os
import sys
import io
import asyncio
import threading
import signal
import yt_dlp
import re
import json
from dotenv import load_dotenv
import logging
import importlib
import math
import time
import shlex
from yt_query_logic import (
    build_yt_music_search_url,
    format_duration,
    is_probable_url,
    is_youtube_link,
    normalize_yt_search_term,
    parse_youtube_url,
    sanitize_query,
)

load_dotenv()

import settings

# Configure logging with UTF-8 encoding to handle emoji and Unicode characters
# Keep a global reference to the wrapper so it is never garbage collected,
# which prevents its __del__ from automatically closing sys.stdout.buffer!
_GLOBAL_UTF8_STREAM = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# Create a UTF-8 stream wrapper for stdout to handle emoji
class UTF8StreamHandler(logging.StreamHandler):
    def __init__(self):
        super().__init__(_GLOBAL_UTF8_STREAM)
        
    def close(self):
        # Override close to prevent closing sys.stdout when handlers are refreshed
        self.acquire()
        try:
            if self.stream:
                self.flush()
            # Do not close the stream!
        finally:
            self.release()


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

class LoggingFFmpegPCMAudio(discord.FFmpegPCMAudio):
    def __init__(self, *args, **kwargs):
        self.frames_read = 0
        kwargs.pop('stderr', None)
        super().__init__(*args, **kwargs)
        
        # Start stderr reader thread if stderr pipe is available
        if hasattr(self, '_process') and self._process and getattr(self._process, 'stderr', None):
            self._stderr_thread = threading.Thread(target=self._log_stderr, daemon=True)
            self._stderr_thread.start()

    def read(self):
        ret = super().read()
        if ret:
            self.frames_read += 1
        return ret

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

_original_close = bot.close
async def _custom_close():
    try:
        import api
        api.is_shutting_down = True
        if hasattr(api, '_api_server') and api._api_server:
            api._api_server.should_exit = True
    except Exception:
        pass
    await _original_close()
bot.close = _custom_close

if settings.CONSOLE_USER_ID is not None:
    bot.owner_id = settings.CONSOLE_USER_ID

ytdl = yt_dlp.YoutubeDL(settings.YTDL_OPTIONS)
search_ytdl = yt_dlp.YoutubeDL(getattr(settings, 'SEARCH_YTDL_OPTIONS', settings.YTDL_OPTIONS))
playlist_ytdl = yt_dlp.YoutubeDL(settings.PLAYLIST_YTDL_OPTIONS)

# GLOBAL VARIABLES
music_state_by_guild = {}
play_next_locks_by_guild = {}
votes_by_guild = {}
active_playlist_prompts_by_guild = {}
next_queue_id = 1
empty_voice_leave_tasks = {}
EMPTY_VOICE_LEAVE_DELAY_SECONDS = getattr(settings, 'EMPTY_VOICE_LEAVE_DELAY_SECONDS', 10)
playback_monitor_tasks = {}
loop_lag_monitor_task = None
console_command_queue = None
console_input_thread = None
console_input_thread_stop = None
console_command_consumer_task = None
console_command_bridge_started = False
voice_recovery_tasks = {}


def cancel_pending_playlist_prompt(guild_id):
    """Cancel and disable any active playlist append prompt for this guild."""
    if guild_id is None:
        return
    old_view = active_playlist_prompts_by_guild.pop(guild_id, None)
    if old_view and not old_view.is_finished():
        old_view.disable_all_items()
        old_view.stop()
        if old_view.message:
            try:
                asyncio.create_task(old_view.on_overridden())
            except Exception:
                pass

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

_blacklist_patterns = load_yt_blacklist_patterns()

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
            'voice_channel_id': None,
            'text_channel_id': None,
            'is_playing': False,
            'was_playing': False,
            'manual_stop': False,
            'saved_at': time.time(),
        }
        music_state_by_guild[guild_id] = state
    return state


def save_state_to_disk():
    """Atomically save persistent music states to disk."""
    state_path = getattr(settings, 'STATE_FILE', None)
    if not state_path:
        return

    try:
        data_to_save = {}
        for guild_id, state in music_state_by_guild.items():
            queue = state.get('queue', [])
            if not queue and state.get('queue_index', -1) == -1 and not state.get('voice_channel_id'):
                continue

            serializable_queue = []
            for s in queue:
                serializable_queue.append({
                    'queue_id': s.get('queue_id'),
                    'type': s.get('type'),
                    'title': s.get('title'),
                    'data': s.get('data'),
                    'duration': s.get('duration'),
                    'requester_id': s.get('requester_id'),
                    'requester_mention': s.get('requester_mention'),
                    'requester_handle': s.get('requester_handle'),
                })

            manual_stop = bool(state.get('manual_stop', False))
            is_playing = False if manual_stop else bool(state.get('current_song') or state.get('is_playing', False))
            saved_at = float(state.get('saved_at') or time.time())

            data_to_save[str(guild_id)] = {
                'queue': serializable_queue,
                'queue_index': state.get('queue_index', -1),
                'loop_enabled': state.get('loop_enabled', False),
                'volume': state.get('volume', settings.DEFAULT_VOLUME),
                'voice_channel_id': None if manual_stop else state.get('voice_channel_id'),
                'text_channel_id': state.get('text_channel_id'),
                'is_playing': is_playing,
                'manual_stop': manual_stop,
                'saved_at': saved_at,
            }

        parent_dir = os.path.dirname(state_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        def _write_to_disk(save_data, final_path):
            try:
                temp_path = f"{final_path}.tmp"
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(save_data, f, indent=2, ensure_ascii=False)
                os.replace(temp_path, final_path)
                logger.debug("Saved queue state to %s (%d guilds)", final_path, len(save_data))
            except Exception as e:
                logger.warning(f"Failed to save queue state: {e}")

        threading.Thread(target=_write_to_disk, args=(data_to_save, state_path), daemon=True).start()
    except Exception as e:
        logger.warning(f"Failed to prepare queue state for saving: {e}")


def load_state_from_disk():
    """Load persistent music states from disk on startup."""
    global next_queue_id
    state_path = getattr(settings, 'STATE_FILE', None)
    if not state_path or not os.path.exists(state_path):
        return

    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return

        max_qid = next_queue_id
        now = time.time()
        max_age = getattr(settings, 'AUTO_RESUME_MAX_AGE_SECONDS', 300)

        for guild_id_str, g_state in data.items():
            try:
                guild_id = int(guild_id_str)
            except ValueError:
                continue

            queue = g_state.get('queue', [])
            for s in queue:
                s['enqueued_perf'] = time.perf_counter()
                qid = s.get('queue_id', 0)
                if qid >= max_qid:
                    max_qid = qid + 1

            saved_at = float(g_state.get('saved_at', 0))
            is_stale = (saved_at <= 0) or ((now - saved_at) > max_age)
            manual_stop = bool(g_state.get('manual_stop', False))
            was_playing = bool(g_state.get('is_playing', False))

            if manual_stop or is_stale:
                was_playing = False
                voice_channel_id = None
                manual_stop = True
            else:
                voice_channel_id = g_state.get('voice_channel_id')

            music_state_by_guild[guild_id] = {
                'queue': queue,
                'queue_index': g_state.get('queue_index', -1),
                'loop_enabled': g_state.get('loop_enabled', False),
                'current_song': None,
                'volume': g_state.get('volume', settings.DEFAULT_VOLUME),
                'voice_channel_id': voice_channel_id,
                'text_channel_id': g_state.get('text_channel_id'),
                'is_playing': False,
                'was_playing': was_playing,
                'manual_stop': manual_stop,
                'saved_at': saved_at if saved_at > 0 else now,
            }

        next_queue_id = max(next_queue_id, max_qid)
        logger.info(f"Loaded persistent music state for {len(music_state_by_guild)} guild(s) from {state_path}")
    except Exception as e:
        logger.warning(f"Failed to load persistent queue state from {state_path}: {e}")


def update_music_state_channels(guild, voice_channel=None, text_channel=None):
    """Save active channel IDs for voice auto-reconnect."""
    state = get_music_state(guild)
    if state is not None:
        if voice_channel is not None:
            state['voice_channel_id'] = getattr(voice_channel, 'id', None)
            state['manual_stop'] = False
        if text_channel is not None:
            state['text_channel_id'] = getattr(text_channel, 'id', None)
        save_state_to_disk()


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
        save_state_to_disk()


def clear_music_state(guild):
    guild_id = getattr(guild, 'id', None)
    if guild_id:
        cancel_voice_recovery(guild_id)
    state = get_music_state(guild)
    if state is not None:
        state['queue'].clear()
        state['queue_index'] = -1
        state['current_song'] = None
        state['is_playing'] = False
        state['was_playing'] = False
        state['voice_channel_id'] = None
        state['text_channel_id'] = None
        state['manual_stop'] = True
        state['saved_at'] = time.time()
        save_state_to_disk()


def cancel_voice_recovery(guild_id):
    """Cancel pending voice recovery task for a guild if active."""
    if guild_id is None:
        return
    task = voice_recovery_tasks.pop(guild_id, None)
    if task and not task.done():
        task.cancel()


def get_channel_listener_count(channel):
    """Count non-bot members in a voice channel."""
    if not channel:
        return 0
    return sum(1 for member in getattr(channel, 'members', []) if not getattr(member, 'bot', False))


def get_non_bot_voice_member_count(voice_client):
    """Count non-bot users in the bot's current voice channel."""
    if not voice_client or not voice_client.channel:
        return 0
    return get_channel_listener_count(voice_client.channel)


class MusicContext:
    """Lightweight context used for background playback resumption and auto-reconnect."""

    def __init__(self, bot, guild, channel=None, author=None):
        self.bot = bot
        self.guild = guild
        self.channel = channel or ConsoleChannel(guild.name if guild else "console")
        self.author = author or getattr(guild, 'me', None)
        self.message = None

    @property
    def voice_client(self):
        return getattr(self.guild, 'voice_client', None) if self.guild else None

    async def send(self, content=None, **kwargs):
        if self.channel:
            try:
                return await self.channel.send(content, **kwargs)
            except Exception as e:
                logger.warning(f"Failed to send context message: {e}")
        return None


def ensure_voice_recovery_task(guild_id, ctx=None):
    """Start a background task to reconnect to voice and resume playback if disconnected."""
    if not settings.AUTO_RECONNECT or not settings.AUTO_RESUME_PLAYBACK:
        return
    if bot.is_closed():
        return

    state = music_state_by_guild.get(guild_id)
    if not state or state.get('manual_stop') or not state.get('voice_channel_id'):
        return
    if not state.get('queue') and not state.get('is_playing'):
        return

    existing = voice_recovery_tasks.get(guild_id)
    if existing and not existing.done():
        return

    async def recover():
        attempts = 0
        max_attempts = settings.VOICE_RECONNECT_ATTEMPTS
        interval = settings.VOICE_RECONNECT_INTERVAL
        state = music_state_by_guild.get(guild_id)
        if not state or state.get('manual_stop') or not state.get('voice_channel_id'):
            return

        voice_channel_id = state.get('voice_channel_id')
        logger.info(f"Voice recovery initiated for guild {guild_id} (channel {voice_channel_id}).")

        while attempts < max_attempts:
            attempts += 1
            try:
                await asyncio.sleep(interval)
                if bot.is_closed():
                    return
                current_state = music_state_by_guild.get(guild_id)
                if not current_state or current_state.get('manual_stop'):
                    logger.info(f"Aborting voice recovery for guild {guild_id}: manual stop requested.")
                    return

                guild = bot.get_guild(guild_id)
                if not guild:
                    continue

                voice_channel = guild.get_channel(voice_channel_id)
                if not voice_channel:
                    continue

                # Abort if no human listeners are in the voice channel
                if get_channel_listener_count(voice_channel) == 0:
                    logger.info(f"Aborting voice recovery for guild {guild_id}: no human listeners in {voice_channel.name}.")
                    return

                if guild.voice_client and guild.voice_client.is_connected():
                    if guild.voice_client.is_playing() or guild.voice_client.is_paused():
                        break
                    active_ctx = ctx or MusicContext(bot=bot, guild=guild, channel=guild.get_channel(current_state.get('text_channel_id')))
                    async with get_play_next_lock(guild_id):
                        await play_next(active_ctx)
                    break

                if guild.voice_client:
                    try:
                        await guild.voice_client.disconnect(force=True)
                    except Exception:
                        pass

                vc = await voice_channel.connect(timeout=settings.CONNECTION_TIMEOUT, reconnect=True)
                if guild is not None:
                    try:
                        guild.voice_client = vc
                    except (AttributeError, TypeError):
                        pass
                await asyncio.sleep(settings.CONNECTION_STABILIZE_DELAY)
                logger.info(f"Voice recovery succeeded for guild {guild.name} ({guild_id}) on attempt {attempts}!")

                text_ch = guild.get_channel(current_state.get('text_channel_id')) if current_state and current_state.get('text_channel_id') else None
                active_ctx = ctx or MusicContext(bot=bot, guild=guild, channel=text_ch)
                if text_ch:
                    await text_ch.send(f"🔄 **Voice connection restored ({voice_channel.name})!** Resuming playlist...")

                async with get_play_next_lock(guild_id):
                    st = get_music_state(guild)
                    if st and st.get('queue'):
                        curr_idx = st.get('queue_index', -1)
                        if curr_idx >= 0:
                            st['queue_index'] = curr_idx - 1
                        await play_next(active_ctx)
                break
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug(f"Voice recovery attempt {attempts}/{max_attempts} for guild {guild_id} failed: {e}")

        voice_recovery_tasks.pop(guild_id, None)

    voice_recovery_tasks[guild_id] = asyncio.create_task(recover())


async def check_and_resume_all_sessions():
    """Check restored guild states after reconnect and resume playback if active."""
    now = time.time()
    max_age = getattr(settings, 'AUTO_RESUME_MAX_AGE_SECONDS', 300)

    for guild_id, state in list(music_state_by_guild.items()):
        try:
            if state.get('manual_stop', False):
                continue

            saved_at = float(state.get('saved_at', 0))
            if saved_at <= 0 or (now - saved_at) > max_age:
                continue

            voice_channel_id = state.get('voice_channel_id')
            queue = state.get('queue', [])
            was_playing = state.get('was_playing', False) or state.get('is_playing', False)

            if not queue or not was_playing or not voice_channel_id:
                continue

            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            voice_channel = guild.get_channel(voice_channel_id)
            if not voice_channel:
                continue

            # Only auto-resume if there are active listeners in that voice channel
            if get_channel_listener_count(voice_channel) == 0:
                logger.info(
                    "Skipping auto-resume for guild %s (%s): no human listeners in voice channel %s.",
                    guild.name, guild_id, voice_channel.name
                )
                continue

            if guild.voice_client and (guild.voice_client.is_playing() or guild.voice_client.is_paused()):
                continue

            text_channel = guild.get_channel(state.get('text_channel_id')) if state.get('text_channel_id') else None
            ctx = MusicContext(bot=bot, guild=guild, channel=text_channel)

            logger.info(f"Auto-resuming session for guild {guild.name} ({guild_id}) in voice channel {voice_channel.name}...")

            if guild.voice_client:
                try:
                    await guild.voice_client.disconnect(force=True)
                except Exception:
                    pass

            vc = await voice_channel.connect(timeout=settings.CONNECTION_TIMEOUT, reconnect=True)
            if guild is not None:
                try:
                    guild.voice_client = vc
                except (AttributeError, TypeError):
                    pass
            await asyncio.sleep(settings.CONNECTION_STABILIZE_DELAY)

            async with get_play_next_lock(guild_id):
                curr_idx = state.get('queue_index', -1)
                if curr_idx >= 0:
                    state['queue_index'] = curr_idx - 1
                await play_next(ctx)

            if text_channel:
                await text_channel.send(
                    f"🔄 **Restored session ({voice_channel.name})!** Resuming playlist ({len(queue)} tracks in queue)."
                )
        except Exception as e:
            logger.warning(f"Failed to auto-resume session for guild {guild_id}: {e}")


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


class ConsoleMessage:
    """Minimal message object returned by ConsoleChannel.send."""

    def __init__(self, content="", author=None, guild=None, channel=None):
        self.content = content
        self.author = author
        self.guild = guild
        self.channel = channel
        self.attachments = []
        self.reference = None
        self.id = int(time.time() * 1000)

    async def edit(self, content=None, **kwargs):
        if content is None and 'embed' in kwargs:
            embed = kwargs['embed']
            parts = []
            if getattr(embed, 'title', None):
                parts.append(f"**{embed.title}**")
            if getattr(embed, 'description', None):
                parts.append(embed.description)
            if getattr(embed, 'footer', None) and getattr(embed.footer, 'text', None):
                parts.append(f"[{embed.footer.text}]")
            content = "\n".join(parts) if parts else str(embed)
        if content is not None:
            label = getattr(self.channel, 'label', 'console')
            print(f"[{label}] {content}")
        return self


class ConsoleChannel:
    """Minimal channel adapter that prints command responses to stdout."""

    def __init__(self, label="console"):
        self.label = label

    async def send(self, content=None, **kwargs):
        if content is None:
            content = kwargs.get('content')
        if content is None and 'embed' in kwargs:
            embed = kwargs['embed']
            parts = []
            if getattr(embed, 'title', None):
                parts.append(f"**{embed.title}**")
            if getattr(embed, 'description', None):
                parts.append(embed.description)
            if getattr(embed, 'footer', None) and getattr(embed.footer, 'text', None):
                parts.append(f"[{embed.footer.text}]")
            content = "\n".join(parts) if parts else str(embed)
        if content is not None:
            print(f"[{self.label}] {content}")
        return ConsoleMessage(content=content, channel=self)


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
        if command_name in {'yt', 'playlist', 'pl', 'blacklist'}:
            query = command_line[len(command_name):].strip()
            if command_name == 'blacklist':
                await command.callback(ctx, pattern=(query or None))
            elif command_name in {'playlist', 'pl'}:
                await command.callback(ctx, query=(query or None))
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

        if command_name in {'queue', 'q'}:
            if len(parts) >= 2:
                try:
                    page_val = int(parts[1])
                except ValueError:
                    await ctx.send(f"❌ `{parts[1]}` is not a valid number.")
                    return
                await command.callback(ctx, page=page_val)
            else:
                await command.callback(ctx)
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

    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("No running asyncio loop found for console bridge.")
        return

    console_command_bridge_started = True
    console_command_queue = asyncio.Queue()
    console_input_thread_stop = threading.Event()
    console_command_consumer_task = asyncio.create_task(consume_console_commands())

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

                    if current_loop and current_loop.is_running():
                        try:
                            asyncio.run_coroutine_threadsafe(console_command_queue.put(raw_line), current_loop)
                        except RuntimeError:
                            break
                except (EOFError, ValueError):
                    # stdin closed or other readline error
                    break
        except KeyboardInterrupt:
            # Handle Ctrl+C in reader thread: trigger clean shutdown
            if current_loop and current_loop.is_running():
                try:
                    current_loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(perform_graceful_shutdown())
                    )
                except RuntimeError:
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

    playback_monitor_tasks[guild_id] = asyncio.create_task(monitor())


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

    loop_lag_monitor_task = asyncio.create_task(monitor_loop_lag())

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




async def get_playable_search_result(search_term, max_results=10):
    """Resolve a search term to the first playable result.

    Searches YouTube Music by default (or standard YouTube if configured/as fallback).
    Uses ignoreerrors and flat extraction for search listing so restricted/unavailable results
    are skipped quickly, then validates each candidate with the main yt-dlp options.
    """
    search_provider = getattr(settings, 'YOUTUBE_SEARCH_PROVIDER', 'youtube_music')
    entries = []
    is_ytm = (search_provider == 'youtube_music')

    if is_ytm:
        music_url = build_yt_music_search_url(search_term)
        try:
            data = await asyncio.to_thread(search_ytdl.extract_info, music_url, False)
            raw_entries = (data.get('entries') or []) if isinstance(data, dict) else []
            for e in raw_entries:
                if not e:
                    continue
                u = e.get('url') or e.get('webpage_url') or ''
                # Skip browse cards (artists, albums, channels) and keep watch URLs / IDs
                if '/browse/' in u:
                    continue
                if 'watch?v=' in u or 'youtu.be/' in u or e.get('id'):
                    entries.append(e)
        except Exception as exc:
            logger.warning("YouTube Music search failed for '%s': %s. Falling back to standard YouTube.", search_term, exc)
            entries = []

    # If YouTube Music returned no playable tracks or standard YouTube is selected, fallback/search standard YouTube
    if not entries:
        if is_ytm:
            logger.info("No candidates from YouTube Music search for '%s', falling back to ytsearch.", search_term)
        search_expr = f"ytsearch{max_results}:{search_term}"
        data = await asyncio.to_thread(search_ytdl.extract_info, search_expr, False)
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
        if not candidate_url or '/browse/' in candidate_url:
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

        # If candidate originated from YouTube Music, ensure the webpage URL reflects it
        if candidate_url.startswith('https://music.youtube.com'):
            candidate_data['music_url'] = candidate_url

        return candidate_data, skip_count

    if last_error:
        raise last_error
    raise ValueError("No playable YouTube results found")


async def ensure_voice_connected(ctx):
    """Ensure bot is connected to user's voice channel. Returns True on success."""
    if ctx.author.voice and ctx.author.voice.channel:
        update_music_state_channels(ctx.guild, voice_channel=ctx.author.voice.channel, text_channel=getattr(ctx, 'channel', None))

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
        update_music_state_channels(ctx.guild, voice_channel=ctx.author.voice.channel, text_channel=getattr(ctx, 'channel', None))
        return True
    except Exception as e:
        logger.error(f"Failed to connect to voice channel: {e}")
        await ctx.send(f"❌ Failed to connect: {e}")
        return False


def make_song(song_type, title, data, requester):
    """Create a queue song object with requester metadata."""
    global next_queue_id
    avatar_url = None
    if requester:
        try:
            if hasattr(requester, 'display_avatar') and requester.display_avatar:
                avatar_url = str(requester.display_avatar.url)
            elif hasattr(requester, 'avatar') and requester.avatar:
                avatar_url = str(requester.avatar.url)
        except Exception:
            avatar_url = None

    song = {
        'queue_id': next_queue_id,
        'type': song_type,
        'title': title,
        'data': data,
        'requester_id': requester.id if requester else None,
        'requester_mention': requester.mention if requester else 'Unknown',
        'requester_handle': str(requester) if requester else 'Unknown',
        'requester_avatar': avatar_url,
        'enqueued_perf': time.perf_counter(),
    }
    next_queue_id += 1
    return song


def is_admin_member(member):
    """Return True ONLY if the Discord member is the bot owner defined by USER_ID in .env."""
    if member is None:
        return False
    if settings.CONSOLE_USER_ID and member.id == settings.CONSOLE_USER_ID:
        return True
    # Commented out: Server admins are no longer automatically MusicBot admins
    # return bool(getattr(member.guild_permissions, 'administrator', False))
    return False


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

            state = get_music_state(guild)
            if state:
                state['manual_stop'] = True
                state['is_playing'] = False
                state['was_playing'] = False
                state['voice_channel_id'] = None
                state['text_channel_id'] = None
                state['saved_at'] = time.time()
            cancel_voice_recovery(guild_id)
            clear_music_state(guild)
            clear_votes(guild_id)
            fresh_voice_client.stop()
            try:
                await asyncio.wait_for(fresh_voice_client.disconnect(force=True), timeout=5.0)
            except Exception as e:
                logger.warning("Voice disconnect timed out/failed: %s", e)
            cancel_voice_recovery(guild_id)
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

    empty_voice_leave_tasks[guild_id] = asyncio.create_task(leave_if_still_empty())


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
    if already_voted:
        votes.remove(user_id)
    else:
        votes.add(user_id)
    return votes, already_voted

def get_skip_votes_info(guild, action_key='skip'):
    class FakeCtx:
        def __init__(self, guild):
            self.guild = guild
            self.voice_client = guild.voice_client
    
    ctx = FakeCtx(guild)
    try:
        required_votes, eligible_count = get_skip_vote_required_count(ctx)
    except Exception:
        return 0, 0, 0
        
    guild_votes = votes_by_guild.get(guild.id, {})
    action_votes = guild_votes.get(action_key, set())
    return len(action_votes), required_votes, eligible_count


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
        music_state['is_playing'] = False
        save_state_to_disk()
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
            music_state['is_playing'] = False
            save_state_to_disk()
            await clear_bot_status_if_idle()
            return

    if next_idx < 0:
        next_idx = 0

    music_state['queue_index'] = next_idx
    song = queue[next_idx]
    music_state['current_song'] = song  # Track currently playing song
    music_state['is_playing'] = True
    music_state['manual_stop'] = False
    save_state_to_disk()
    queue_wait_ms = int((time.perf_counter() - song.get('enqueued_perf', time.perf_counter())) * 1000)

    try:
        logger.debug(f"Now playing - {song['type']}: {song['title']}")
        # 1. Create the base Source
        if song['type'] == 'youtube':
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
            current_loop = asyncio.get_running_loop()

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
                        current_state = get_music_state(ctx.guild)
                        if not current_state or current_state.get('manual_stop') or bot.is_closed():
                            return

                        if ctx.voice_client and ctx.voice_client.is_connected():
                            await play_next(ctx)
                        elif ctx.guild and settings.AUTO_RECONNECT:
                            if current_state.get('queue') or current_state.get('is_playing'):
                                logger.warning(f"Voice client lost connection in guild {ctx.guild.id}. Starting voice recovery...")
                                ensure_voice_recovery_task(ctx.guild.id, ctx)

                future = asyncio.run_coroutine_threadsafe(next_with_lock(), current_loop)

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
            current_state = get_music_state(ctx.guild)
            if ctx.guild and settings.AUTO_RECONNECT and current_state and not current_state.get('manual_stop') and not bot.is_closed():
                ensure_voice_recovery_task(ctx.guild.id, ctx)

    except Exception as e:
        logger.error(f"Error in play_next: {e}", exc_info=True)
        await ctx.send(f"❌ Error playing: {e}")
        # Try next song after a small delay
        await asyncio.sleep(1)
        current_state = get_music_state(ctx.guild)
        if current_state and not current_state.get('manual_stop') and not bot.is_closed():
            if ctx.voice_client and ctx.voice_client.is_connected():
                async with get_play_next_lock(getattr(ctx.guild, 'id', None)):
                    await play_next(ctx)
            elif ctx.guild and settings.AUTO_RECONNECT:
                ensure_voice_recovery_task(ctx.guild.id, ctx)

# --- EVENTS ---

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user}')
    start_console_command_bridge()
    if settings.AUTO_RESUME_PLAYBACK:
        await check_and_resume_all_sessions()

@bot.event
async def on_resumed():
    logger.info('Gateway session resumed.')
    if settings.AUTO_RESUME_PLAYBACK:
        await check_and_resume_all_sessions()

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
        state = get_music_state(ctx.guild)
        if state:
            state['manual_stop'] = False
        update_music_state_channels(ctx.guild, voice_channel=channel, text_channel=getattr(ctx, 'channel', None))
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



class PlaylistAppendView(discord.ui.View):
    """Interactive Discord UI View prompting if the user wants to add remaining playlist tracks."""

    def __init__(self, author_id, guild_id, playlist_title, remaining_entries, ctx, timeout=None):
        if timeout is None:
            timeout = settings.PLAYLIST_CONFIRMATION_TIMEOUT
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.guild_id = guild_id
        self.playlist_title = playlist_title
        self.remaining_entries = remaining_entries
        self.ctx = ctx
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "❌ Only the person who requested this song can add the playlist.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Add Remaining Playlist", style=discord.ButtonStyle.success, emoji="📑")
    async def add_playlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        active_playlist_prompts_by_guild.pop(self.guild_id, None)
        self.disable_all_items()
        self.stop()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass
        await interaction.response.defer()
        await enqueue_playlist_tracks(self.ctx, self.playlist_title, self.remaining_entries)

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, emoji="❌")
    async def dismiss_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        active_playlist_prompts_by_guild.pop(self.guild_id, None)
        self.disable_all_items()
        self.stop()
        if self.message:
            try:
                await self.message.edit(view=None)
            except Exception:
                pass
        await interaction.response.defer()

    def disable_all_items(self):
        for item in self.children:
            if hasattr(item, 'disabled'):
                item.disabled = True

    async def on_timeout(self):
        active_playlist_prompts_by_guild.pop(self.guild_id, None)
        self.disable_all_items()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass

    async def on_overridden(self):
        self.disable_all_items()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


async def enqueue_single_song_from_url(ctx, title, webpage_url, video_data=None):
    """Enqueue a single YouTube track."""
    if not video_data and not title:
        video_data = await asyncio.to_thread(ytdl.extract_info, webpage_url, False)

    if video_data:
        title = title or video_data.get('title') or "YouTube Track"
        webpage_url = video_data.get('webpage_url') or webpage_url

    title = title or "YouTube Track"

    if is_blacklisted_title(title):
        await ctx.send("❌ This song is in the blacklist.")
        return None

    song_obj = make_song('youtube', title, webpage_url, ctx.author)
    if video_data:
        if video_data.get('url'):
            song_obj['stream_url'] = video_data.get('url')
            song_obj['stream_url_cached_at'] = time.time()
        song_obj['format_id'] = video_data.get('format_id')
        if video_data.get('thumbnail'):
            song_obj['thumbnail'] = video_data.get('thumbnail')
            
        artist = video_data.get('artist') or video_data.get('creator') or video_data.get('uploader') or video_data.get('channel')
        if artist:
            song_obj['artist'] = artist
            
        song_obj['ext'] = video_data.get('ext')
        song_obj['duration'] = video_data.get('duration')

    queue = get_music_queue(ctx.guild)
    queue.append(song_obj)
    save_state_to_disk()

    if not ctx.voice_client:
        await ctx.send("❌ Lost voice connection.")
        return None

    async with get_play_next_lock(getattr(ctx.guild, 'id', None)):
        if not ctx.voice_client.is_playing():
            await play_next(ctx)
        else:
            await ctx.send(f"✅ Added to queue: `{title}` (added by {ctx.author.mention})")

    return song_obj


async def enqueue_playlist_tracks(ctx, playlist_title, raw_entries):
    """Enqueue multiple tracks from a playlist."""
    max_items = settings.MAX_PLAYLIST_ITEMS
    entries_to_process = raw_entries[:max_items]

    skipped_blacklist = 0
    skipped_invalid = 0
    added_songs = []

    queue = get_music_queue(ctx.guild)

    for entry in entries_to_process:
        if not entry:
            skipped_invalid += 1
            continue

        entry_title = entry.get('title') or "YouTube Track"
        if is_blacklisted_title(entry_title):
            skipped_blacklist += 1
            continue

        entry_url = entry.get('webpage_url') or entry.get('url')
        if not entry_url and entry.get('id'):
            entry_url = f"https://www.youtube.com/watch?v={entry['id']}"

        if not entry_url:
            skipped_invalid += 1
            continue

        song_obj = make_song('youtube', entry_title, entry_url, ctx.author)
        song_obj['duration'] = entry.get('duration')
        if entry.get('thumbnail'):
            song_obj['thumbnail'] = entry.get('thumbnail')
            
        artist = entry.get('artist') or entry.get('creator') or entry.get('uploader') or entry.get('channel')
        if artist:
            song_obj['artist'] = artist
            
        queue.append(song_obj)
        added_songs.append(song_obj)

    total_added = len(added_songs)
    if total_added == 0:
        return await ctx.send("❌ No playable songs could be added from this playlist (all tracks were filtered or unavailable).")

    save_state_to_disk()

    if not ctx.voice_client:
        return await ctx.send("❌ Lost voice connection.")

    async with get_play_next_lock(getattr(ctx.guild, 'id', None)):
        if not ctx.voice_client.is_playing():
            await play_next(ctx)

    status_msg = f"📑 Added **{total_added}** song(s) from playlist **{playlist_title}** to queue! (added by {ctx.author.mention})"
    skipped_total = skipped_blacklist + skipped_invalid
    if skipped_total > 0:
        skip_details = []
        if skipped_blacklist > 0:
            skip_details.append(f"{skipped_blacklist} blacklisted")
        if skipped_invalid > 0:
            skip_details.append(f"{skipped_invalid} unavailable")
        status_msg += f"\nℹ️ Skipped {', '.join(skip_details)} track(s)."
    if len(raw_entries) > max_items:
        status_msg += f"\n⚠️ Playlist capped at maximum allowed {max_items} tracks."

    await ctx.send(status_msg)


async def check_and_prompt_playlist_on_the_side(ctx, playlist_url, enqueued_id, enqueued_title, single_video_url, initial_playlist_data=None):
    """Background task that checks for playlist tracks and presents a non-intrusive prompt on the side."""
    try:
        playlist_data = initial_playlist_data
        if playlist_data is None:
            playlist_data = await asyncio.to_thread(playlist_ytdl.extract_info, playlist_url, False)

        if not playlist_data or not isinstance(playlist_data, dict):
            return

        playlist_title = playlist_data.get('title') or "YouTube Playlist"
        raw_entries = [e for e in playlist_data.get('entries', []) if e]

        # Filter out the track that was already enqueued
        remaining_entries = []
        for e in raw_entries:
            if not e:
                continue
            e_id = e.get('id')
            e_url = e.get('url') or e.get('webpage_url')
            if enqueued_id and e_id == enqueued_id:
                continue
            if single_video_url and (e_url == single_video_url):
                continue
            remaining_entries.append(e)

        remaining_count = len(remaining_entries)
        if remaining_count == 0:
            return

        view = PlaylistAppendView(
            author_id=ctx.author.id,
            guild_id=getattr(ctx.guild, 'id', None),
            playlist_title=playlist_title,
            remaining_entries=remaining_entries,
            ctx=ctx,
            timeout=settings.PLAYLIST_CONFIRMATION_TIMEOUT,
        )
        view.add_playlist_button.label = f"Add Remaining ({remaining_count} tracks)"

        if ctx.guild:
            active_playlist_prompts_by_guild[ctx.guild.id] = view

        if isinstance(ctx.channel, ConsoleChannel):
            await ctx.send(
                f"📑 **Playlist detected:** `{playlist_title}` ({remaining_count} more tracks).\n"
                f"👉 Type **!addplaylist** (or **!addpl**) to add the rest to the queue!"
            )
            return

        prompt_msg = await ctx.send(embed=embed, view=view)
        view.message = prompt_msg

    except Exception as e:
        logger.debug("Silently ignored unviewable or unsupported playlist on the side: %s", e)


async def process_youtube_playlist(ctx, parsed_yt):
    """Plays the single song immediately, then presents a non-blocking prompt on the side to add the remaining playlist tracks."""
    playlist_url = parsed_yt['playlist_url']
    single_video_url = parsed_yt['single_video_url']

    # Invalidate any prior playlist prompt in this guild
    if ctx.guild:
        cancel_pending_playlist_prompt(ctx.guild.id)

    enqueued_song = None
    enqueued_id = None
    playlist_data = None

    if single_video_url:
        await ctx.send("🔗 Loading link...")
        video_data = await asyncio.to_thread(ytdl.extract_info, single_video_url, False)
        if not video_data:
            return await ctx.send("❌ Error: Could not load song from link.")

        enqueued_id = video_data.get('id') or parsed_yt.get('video_id')
        enqueued_song = await enqueue_single_song_from_url(
            ctx,
            title=video_data.get('title'),
            webpage_url=video_data.get('webpage_url') or single_video_url,
            video_data=video_data,
        )
        if not enqueued_song:
            return

        # Check playlist in background on the side so song plays without any delay
        if playlist_url:
            asyncio.create_task(
                check_and_prompt_playlist_on_the_side(
                    ctx=ctx,
                    playlist_url=playlist_url,
                    enqueued_id=enqueued_id,
                    enqueued_title=enqueued_song['title'],
                    single_video_url=single_video_url,
                    initial_playlist_data=None,
                )
            )
    else:
        # Pure playlist link without single video - fetch playlist to find first playable song
        await ctx.send("🔗 Loading link...")
        playlist_data = await asyncio.to_thread(playlist_ytdl.extract_info, playlist_url, False)
        if not playlist_data:
            return await ctx.send("❌ Could not retrieve playlist information.")

        raw_entries = [e for e in playlist_data.get('entries', []) if e]
        if not raw_entries:
            return await ctx.send("❌ The playlist is empty or unavailable.")

        first_entry = raw_entries[0]
        first_title = first_entry.get('title') or "YouTube Track"
        first_url = first_entry.get('webpage_url') or first_entry.get('url')
        if not first_url and first_entry.get('id'):
            first_url = f"https://www.youtube.com/watch?v={first_entry['id']}"

        enqueued_id = first_entry.get('id')
        enqueued_song = await enqueue_single_song_from_url(ctx, title=first_title, webpage_url=first_url)
        if not enqueued_song:
            return

        # Check remaining tracks on the side
        asyncio.create_task(
            check_and_prompt_playlist_on_the_side(
                ctx=ctx,
                playlist_url=playlist_url,
                enqueued_id=enqueued_id,
                enqueued_title=enqueued_song['title'],
                single_video_url=first_url,
                initial_playlist_data=playlist_data,
            )
        )


@bot.command()
async def yt(ctx, *, query):
    """Plays from YouTube (URL or search). Usage: !yt <url or search query>"""
    if not await ensure_voice_connected(ctx):
        return

    # Verify connection
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        return await ctx.send("❌ Failed to connect to voice channel.")

    # Invalidate any pending playlist prompt in this guild
    if ctx.guild:
        cancel_pending_playlist_prompt(ctx.guild.id)

    # 1. Check if query is a YouTube playlist link
    parsed_yt = parse_youtube_url(query)
    if parsed_yt['is_playlist']:
        if settings.DETECT_PLAYLISTS:
            return await process_youtube_playlist(ctx, parsed_yt)
        elif parsed_yt.get('single_video_url'):
            query = parsed_yt['single_video_url']

    # If query is a search URL (e.g. music.youtube.com/search?q=... or youtube.com/results?search_query=...)
    if parsed_yt.get('is_search') and parsed_yt.get('search_term'):
        query = parsed_yt['search_term']
        parsed_yt = parse_youtube_url(query)

    # 2. Determine if input is direct URL (YouTube single or generic) or a search term.
    #    Lyrics normalization is only for search terms.
    if parsed_yt['is_youtube']:
        search_query = parsed_yt['clean_url']
        await ctx.send("🔗 Loading link...")
        query_type = 'url'
    elif is_probable_url(query):
        search_query = sanitize_query(query)
        await ctx.send("🔗 Loading link...")
        query_type = 'url'
    else:
        effective_search_term, _ = normalize_yt_search_term(query)
        if is_blacklisted_title(query) or is_blacklisted_title(effective_search_term):
            return await ctx.send("❌ This song is in the blacklist.")
        search_provider = getattr(settings, 'YOUTUBE_SEARCH_PROVIDER', 'youtube_music')
        engine_label = "YouTube Music" if search_provider == 'youtube_music' else "YouTube"
        await ctx.send(f"🔎 Searching {engine_label} for: **{query}**...")
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
        webpage_url = video_data.get('music_url') or video_data.get('webpage_url') or video_data.get('url')
        
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
        if video_data.get('thumbnail'):
            song_obj['thumbnail'] = video_data.get('thumbnail')
            
        artist = video_data.get('artist') or video_data.get('creator') or video_data.get('uploader') or video_data.get('channel')
        if artist:
            song_obj['artist'] = artist
            
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


@bot.command(aliases=['addpl', 'apl'])
async def addplaylist(ctx):
    """Add the remaining tracks from a recently detected playlist or mix to the queue."""
    if not ctx.guild:
        return await ctx.send("❌ This command can only be used in a server.")

    prompt_data = active_playlist_prompts_by_guild.get(ctx.guild.id)
    if not prompt_data:
        return await ctx.send("❌ No active playlist prompt found (it may have expired or been overridden).")

    # Invalidate the active prompt
    cancel_pending_playlist_prompt(ctx.guild.id)

    playlist_title = getattr(prompt_data, 'playlist_title', 'Playlist')
    remaining_entries = getattr(prompt_data, 'remaining_entries', [])

    if not remaining_entries:
        return await ctx.send("❌ No remaining tracks found to add.")

    await enqueue_playlist_tracks(ctx, playlist_title, remaining_entries)


@bot.command(aliases=['pl'])
async def playlist(ctx, *, query: str = None):
    """Views the queue, loads a YouTube playlist, or accepts a pending playlist prompt."""
    if query is None:
        if ctx.guild and ctx.guild.id in active_playlist_prompts_by_guild:
            return await addplaylist(ctx)
        return await queue(ctx)

    if query.strip().lower() in {'add', 'accept', 'yes', 'y'}:
        return await addplaylist(ctx)

    if not await ensure_voice_connected(ctx):
        return

    # Verify connection
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        return await ctx.send("❌ Failed to connect to voice channel.")

    parsed_yt = parse_youtube_url(query)
    if parsed_yt['is_playlist']:
        if settings.DETECT_PLAYLISTS:
            return await process_youtube_playlist(ctx, parsed_yt)
        elif parsed_yt.get('single_video_url'):
            query = parsed_yt['single_video_url']

    return await yt(ctx, query=query)


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


@bot.command(aliases=['norm'])
async def normalize(ctx, state: str = None):
    """View or toggle EBU R128 audio loudness normalization. Usage: !normalize [on/off]"""
    if not await enforce_command_access(ctx, 'normalize'):
        return

    current = getattr(settings, 'NORMALIZE_AUDIO', True)
    target_lufs = getattr(settings, 'TARGET_LUFS', -16.0)
    true_peak = getattr(settings, 'TRUE_PEAK', -1.5)

    if state is None:
        status = "enabled" if current else "disabled"
        return await ctx.send(
            f"🔊 Audio normalization (EBU R128 loudnorm) is **{status}**.\n"
            f"• Target Loudness: `{target_lufs} LUFS`\n"
            f"• True Peak Limit: `{true_peak} dBTP`\n"
            f"Usage: `{settings.COMMAND_PREFIX}normalize on` or `{settings.COMMAND_PREFIX}normalize off`"
        )

    val = state.strip().lower()
    if val in {'on', 'enable', 'true', '1'}:
        settings.update_normalize_audio(True)
        await ctx.send(
            f"✅ Audio normalization **enabled** (EBU R128 loudnorm: `{target_lufs} LUFS`, peak `{true_peak} dBTP`).\n"
            f"Applies to all upcoming tracks."
        )
    elif val in {'off', 'disable', 'false', '0'}:
        settings.update_normalize_audio(False)
        await ctx.send("⚠️ Audio normalization **disabled**. Songs will play at raw loudness.")
    else:
        await ctx.send(f"❌ Invalid option `{state}`. Use `{settings.COMMAND_PREFIX}normalize on` or `{settings.COMMAND_PREFIX}normalize off`.")


@bot.command()
async def skip(ctx):
    """Skips current song based on configured permissions and vote rules."""
    if not await ensure_voice_connected(ctx):
        return

    if not ctx.voice_client.is_playing() and not get_music_queue(ctx.guild):
        await ctx.send("❌ Nothing is playing and the queue is empty.")
        return

    mode = get_command_mode('skip')
    vote_cfg = settings.get_skip_vote_config()
    force_vote_for_admin = vote_cfg.get('force_vote_for_admin', False)

    async def execute_skip(msg):
        clear_votes(ctx.guild.id, action_key='skip')
        await ctx.send(msg)
        if ctx.voice_client.is_playing():
            ctx.voice_client.stop()
        else:
            async with get_play_next_lock(getattr(ctx.guild, 'id', None)):
                await play_next(ctx)

    if is_admin_member(ctx.author) and not force_vote_for_admin:
        await execute_skip("⏭️ Skipped by admin.")
        return

    # Let the requester skip their own currently playing song directly.
    current_song = get_current_song(ctx.guild)
    if current_song and current_song.get('requester_id') == ctx.author.id:
        await execute_skip("⏭️ Skipped your own song.")
        return

    if mode == 'admin_only':
        await ctx.send("❌ Only administrators can use this command.")
        return

    if mode == 'open':
        await execute_skip("⏭️ Skipped.")
        return

    if vote_cfg['same_channel_only']:
        if not ctx.author.voice or not ctx.voice_client.channel or ctx.author.voice.channel != ctx.voice_client.channel:
            await ctx.send("❌ You must be in the same voice channel as the bot to vote skip.")
            return

    required_votes, eligible_count = get_skip_vote_required_count(ctx)
    votes, already_voted = register_vote(ctx.guild.id, 'skip', ctx.author.id)
    current_votes = len(votes)

    if already_voted:
        await ctx.send(f"🗳️ You removed your vote to skip. Votes: **{current_votes}/{required_votes}**")
        return

    if current_votes >= required_votes:
        await execute_skip(f"⏭️ Vote passed (**{current_votes}/{required_votes}** of {eligible_count} listeners). Skipping.")
        return

    await ctx.send(f"🗳️ Skip vote added (**{current_votes}/{required_votes}** of {eligible_count} listeners).")

class QueuePaginatorView(discord.ui.View):
    """Interactive Discord UI View providing book-like page navigation for the music queue."""

    def __init__(self, ctx, current_page: int = 1, timeout: int = None):
        if timeout is None:
            timeout = settings.QUEUE_PAGINATOR_TIMEOUT
        super().__init__(timeout=timeout)
        self.ctx = ctx
        self.current_page = current_page
        self.items_per_page = settings.QUEUE_ITEMS_PER_PAGE
        self.message = None
        self.update_buttons()

    def get_queue_state(self):
        queue = get_music_queue(self.ctx.guild)
        music_state = get_music_state(self.ctx.guild)
        curr_idx = music_state.get('queue_index', -1) if music_state else -1
        loop_enabled = music_state.get('loop_enabled', False) if music_state else False
        return queue, curr_idx, loop_enabled

    def get_total_pages(self, total_songs: int) -> int:
        if total_songs == 0:
            return 1
        return max(1, math.ceil(total_songs / self.items_per_page))

    def build_embed(self) -> discord.Embed:
        queue, curr_idx, loop_enabled = self.get_queue_state()
        total_songs = len(queue)

        if total_songs == 0:
            embed = discord.Embed(
                title="📑 Music Queue",
                description="The queue is currently empty.",
                color=discord.Color.blurple(),
            )
            return embed

        total_pages = self.get_total_pages(total_songs)
        if self.current_page > total_pages:
            self.current_page = total_pages
        if self.current_page < 1:
            self.current_page = 1

        start_idx = (self.current_page - 1) * self.items_per_page
        end_idx = min(start_idx + self.items_per_page, total_songs)
        page_songs = queue[start_idx:end_idx]

        # Calculate total queue duration
        total_duration_sec = 0.0
        has_duration = False
        for s in queue:
            dur = s.get('duration')
            if dur is not None:
                try:
                    total_duration_sec += float(dur)
                    has_duration = True
                except (ValueError, TypeError):
                    pass
        total_duration_str = format_duration(total_duration_sec) if has_duration and total_duration_sec > 0 else ""

        # Now playing header
        current_song = get_current_song(self.ctx.guild)
        header = ""
        if current_song:
            np_dur = f" `[{format_duration(current_song.get('duration'))}]`" if current_song.get('duration') else ""
            header = f"▶️ **Now Playing:** {current_song['title']}{np_dur}\n\n"

        lines = []
        for i, song in enumerate(page_songs, start=start_idx + 1):
            is_playing = (i - 1 == curr_idx)
            marker = "▶️ " if is_playing else ""
            dur_str = f" `[{format_duration(song.get('duration'))}]`" if song.get('duration') else ""
            requester = song.get('requester_handle') or song.get('requester_mention') or "unknown"

            title = song.get('title', 'Unknown Track')
            if len(title) > 70:
                title = title[:67] + "..."

            lines.append(f"`{i}.` {marker}**{title}**{dur_str}\n    └ Added by {requester}")

        body = "\n".join(lines) if lines else "No songs on this page."
        description = f"{header}**Up Next (Tracks {start_idx + 1} - {end_idx}):**\n{body}"

        embed = discord.Embed(
            title="📑 Music Queue",
            description=description,
            color=discord.Color.blurple(),
        )

        footer_parts = [
            f"Page {self.current_page} of {total_pages}",
            f"{total_songs} track{'s' if total_songs != 1 else ''}",
        ]
        if total_duration_str:
            footer_parts.append(f"Total: {total_duration_str}")
        if loop_enabled:
            footer_parts.append("🔁 Loop: ON")

        embed.set_footer(text=" • ".join(footer_parts))
        return embed

    def update_buttons(self):
        queue, _, _ = self.get_queue_state()
        total_pages = self.get_total_pages(len(queue))

        if self.current_page > total_pages:
            self.current_page = total_pages
        if self.current_page < 1:
            self.current_page = 1

        self.first_page_button.disabled = (self.current_page <= 1)
        self.prev_page_button.disabled = (self.current_page <= 1)
        self.page_indicator_button.label = f"{self.current_page}/{total_pages}"
        self.next_page_button.disabled = (self.current_page >= total_pages)
        self.last_page_button.disabled = (self.current_page >= total_pages)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.ctx.guild and interaction.guild and interaction.guild.id != self.ctx.guild.id:
            await interaction.response.send_message("❌ This queue is for another server.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def first_page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 1
        self.update_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.primary, row=0)
    async def prev_page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 1:
            self.current_page -= 1
        self.update_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True, row=0)
    async def page_indicator_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.primary, row=0)
    async def next_page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue, _, _ = self.get_queue_state()
        total_pages = self.get_total_pages(len(queue))
        if self.current_page < total_pages:
            self.current_page += 1
        self.update_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def last_page_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        queue, _, _ = self.get_queue_state()
        total_pages = self.get_total_pages(len(queue))
        self.current_page = total_pages
        self.update_buttons()
        embed = self.build_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Close", emoji="🗑️", style=discord.ButtonStyle.danger, row=1)
    async def dismiss_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        try:
            await interaction.message.delete()
        except Exception:
            self.disable_all_items()
            await interaction.response.edit_message(view=None)

    def disable_all_items(self):
        for item in self.children:
            if hasattr(item, 'disabled'):
                item.disabled = True

    async def on_timeout(self):
        self.disable_all_items()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


@bot.command(aliases=['q'])
async def queue(ctx, page: int = 1):
    """Lists the current queue with interactive book-like pagination. Usage: !queue [page]"""
    queue_list = get_music_queue(ctx.guild)
    if not queue_list:
        await ctx.send("The queue is currently empty.")
        return

    items_per_page = settings.QUEUE_ITEMS_PER_PAGE
    total_pages = max(1, math.ceil(len(queue_list) / items_per_page))

    if page < 1 or page > total_pages:
        await ctx.send(f"❌ Invalid page number. Please choose a page between 1 and {total_pages}.")
        return

    view = QueuePaginatorView(ctx=ctx, current_page=page, timeout=settings.QUEUE_PAGINATOR_TIMEOUT)
    embed = view.build_embed()

    if isinstance(ctx.channel, ConsoleChannel):
        await ctx.send(embed=embed)
        return

    msg = await ctx.send(embed=embed, view=view)
    view.message = msg


@bot.command()
async def current(ctx):
    """Shows the currently playing song."""
    current_song = get_current_song(ctx.guild)
    if not current_song:
        await ctx.send("❌ No song is currently playing.")
        return

    dur_str = f" `[{format_duration(current_song.get('duration'))}]`" if current_song.get('duration') else ""
    await ctx.send(
        f"🎶 **Current Song:** {current_song['title']}{dur_str} "
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
    save_state_to_disk()
    
    status = "enabled" if music_state['loop_enabled'] else "disabled"
    await ctx.send(f"🔁 Playlist looping is now **{status}**.")

@bot.command()
async def skipto(ctx, index: int):
    """Skips to a specific number in the queue. Usage: !skipto <position>"""
    if not await enforce_command_access(ctx, 'skipto'):
        return

    if not await ensure_voice_connected(ctx):
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
        save_state_to_disk()

    if not ctx.voice_client.is_playing():
        await ctx.send(f"⏭️ Skipped to position **{index}**.")
        async with get_play_next_lock(getattr(ctx.guild, 'id', None)):
            await play_next(ctx)
    else:
        # Stop the current song. This triggers 'play_next', which pulls the song at next_index.
        ctx.voice_client.stop()
        await ctx.send(f"⏭️ Skipped to position **{index}**.")

@bot.command()
async def clear(ctx):
    """Clears all upcoming songs in the queue."""
    if not await enforce_command_access(ctx, 'clear'):
        return

    guild_id = getattr(ctx.guild, 'id', None)
    if guild_id:
        cancel_voice_recovery(guild_id)

    state = get_music_state(ctx.guild)
    if state:
        state['manual_stop'] = True
        state['is_playing'] = False
        state['was_playing'] = False
        state['voice_channel_id'] = None
        state['text_channel_id'] = None
        state['saved_at'] = time.time()

    clear_music_state(ctx.guild)
    if ctx.voice_client:
        ctx.voice_client.stop()
        if guild_id:
            cancel_voice_recovery(guild_id)

    if ctx.guild:
        clear_votes(ctx.guild.id)
    await ctx.send("🗑️ **Playlist cleared.**")
@bot.command()
async def pause(ctx):
    """Pauses the current playing song."""
    if not await ensure_voice_connected(ctx):
        return

    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("❌ Nothing is playing to pause.")
        return

    mode = get_command_mode('pause')
    vote_cfg = settings.get_skip_vote_config()
    force_vote_for_admin = vote_cfg.get('force_vote_for_admin', False)

    async def execute_pause(msg):
        clear_votes(ctx.guild.id, action_key='pause')
        ctx.voice_client.pause()
        await ctx.send(msg)

    if is_admin_member(ctx.author) and not force_vote_for_admin:
        await execute_pause("⏸️ **Paused** by admin.")
        return

    if mode == 'admin_only':
        await ctx.send("❌ Only administrators can use this command.")
        return

    if mode == 'open':
        await execute_pause("⏸️ **Paused**.")
        return
        
    if vote_cfg['same_channel_only']:
        if not ctx.author.voice or not ctx.voice_client.channel or ctx.author.voice.channel != ctx.voice_client.channel:
            await ctx.send("❌ You must be in the same voice channel as the bot to vote.")
            return
            
    required_votes, eligible_count = get_skip_vote_required_count(ctx)
    votes, already_voted = register_vote(ctx.guild.id, 'pause', ctx.author.id)
    current_votes = len(votes)

    if already_voted:
        await ctx.send(f"🗳️ You removed your vote to pause. Votes: **{current_votes}/{required_votes}**")
        return

    if current_votes >= required_votes:
        await execute_pause(f"⏸️ Vote passed (**{current_votes}/{required_votes}** of {eligible_count} listeners). **Paused**.")
        return

    await ctx.send(f"🗳️ Pause vote added (**{current_votes}/{required_votes}** of {eligible_count} listeners).")

@bot.command()
async def resume(ctx):
    """Resumes the paused song."""
    if not await ensure_voice_connected(ctx):
        return

    if not ctx.voice_client:
        return

    mode = get_command_mode('resume')
    vote_cfg = settings.get_skip_vote_config()
    force_vote_for_admin = vote_cfg.get('force_vote_for_admin', False)
    
    is_paused = ctx.voice_client.is_paused()
    
    async def execute_resume(msg):
        clear_votes(ctx.guild.id, action_key='resume')
        if is_paused:
            ctx.voice_client.resume()
            await ctx.send(msg)
        else:
            queue = get_music_queue(ctx.guild)
            if queue and not ctx.voice_client.is_playing():
                await ctx.send(msg)
                async with get_play_next_lock(getattr(ctx.guild, 'id', None)):
                    await play_next(ctx)
            else:
                await ctx.send("❌ Nothing to resume.")

    if not is_paused and (not get_music_queue(ctx.guild) or ctx.voice_client.is_playing()):
        await ctx.send("❌ Nothing is paused or stopped to resume.")
        return

    if is_admin_member(ctx.author) and not force_vote_for_admin:
        await execute_resume("▶️ **Resumed** by admin.")
        return

    if mode == 'admin_only':
        await ctx.send("❌ Only administrators can use this command.")
        return

    if mode == 'open':
        await execute_resume("▶️ **Resumed**.")
        return
        
    if vote_cfg['same_channel_only']:
        if not ctx.author.voice or not ctx.voice_client.channel or ctx.author.voice.channel != ctx.voice_client.channel:
            await ctx.send("❌ You must be in the same voice channel as the bot to vote.")
            return
            
    required_votes, eligible_count = get_skip_vote_required_count(ctx)
    votes, already_voted = register_vote(ctx.guild.id, 'resume', ctx.author.id)
    current_votes = len(votes)

    if already_voted:
        await ctx.send(f"🗳️ You removed your vote to resume. Votes: **{current_votes}/{required_votes}**")
        return

    if current_votes >= required_votes:
        await execute_resume(f"▶️ Vote passed (**{current_votes}/{required_votes}** of {eligible_count} listeners). **Resumed**.")
        return

    await ctx.send(f"🗳️ Resume vote added (**{current_votes}/{required_votes}** of {eligible_count} listeners).")

@bot.command()
async def stop(ctx):
    """Stops playback and disconnects from voice channel."""
    if not await ensure_voice_connected(ctx):
        return

    mode = get_command_mode('stop')
    vote_cfg = settings.get_skip_vote_config()
    force_vote_for_admin = vote_cfg.get('force_vote_for_admin', False)

    async def execute_stop(msg):
        guild_id = getattr(ctx.guild, 'id', None)
        if guild_id:
            cancel_voice_recovery(guild_id)
            cancel_empty_voice_leave_timer(guild_id)
            clear_votes(guild_id)
            cancel_playback_monitor(guild_id)

        state = get_music_state(ctx.guild)
        if state:
            state['manual_stop'] = True
            state['is_playing'] = False
            state['was_playing'] = False
            state['voice_channel_id'] = None
            state['text_channel_id'] = None
            state['saved_at'] = time.time()

        clear_music_state(ctx.guild)

        if ctx.voice_client:
            ctx.voice_client.stop()
            try:
                await asyncio.wait_for(ctx.voice_client.disconnect(force=True), timeout=5.0)
            except Exception as e:
                logger.warning("Stop disconnect timed out/failed: %s", e)
            if guild_id:
                cancel_voice_recovery(guild_id)
            await clear_bot_status_if_idle()
            await ctx.send(msg)
        else:
            await ctx.send("❌ Not connected to a voice channel.")

    if is_admin_member(ctx.author) and not force_vote_for_admin:
        await execute_stop("🛑 Stopped by admin.")
        return

    if mode == 'admin_only':
        await ctx.send("❌ Only administrators can use this command.")
        return

    if mode == 'open':
        await execute_stop("🛑 Stopped and disconnected.")
        return

    if vote_cfg['same_channel_only']:
        if not ctx.author.voice or not ctx.voice_client.channel or ctx.author.voice.channel != ctx.voice_client.channel:
            await ctx.send("❌ You must be in the same voice channel as the bot to vote stop.")
            return

    required_votes, eligible_count = get_skip_vote_required_count(ctx)
    votes, already_voted = register_vote(ctx.guild.id, 'stop', ctx.author.id)
    current_votes = len(votes)

    if already_voted:
        await ctx.send(f"🗳️ You removed your vote to stop. Votes: **{current_votes}/{required_votes}**")
        return

    if current_votes >= required_votes:
        await execute_stop(f"🛑 Vote passed (**{current_votes}/{required_votes}** of {eligible_count} listeners). Stopping.")
        return

    await ctx.send(f"🗳️ Stop vote added (**{current_votes}/{required_votes}** of {eligible_count} listeners).")

@bot.command()
async def remove(ctx, index: int):
    """Removes a song from queue by index. Owner/admin can remove directly; others require vote."""
    mode = get_command_mode('remove')

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

    async def execute_remove(msg, current_index):
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
            save_state_to_disk()
        if ctx.guild:
            clear_votes(ctx.guild.id, action_key=f"remove:{removed_song['queue_id']}")
        await ctx.send(f"{msg} Removed `#{index}`: **{removed_song['title']}**")

    if (is_admin and not force_vote_for_admin) or is_owner:
        await execute_remove("✅", index - 1)
        return

    if mode == 'admin_only':
        await ctx.send("❌ Only administrators can use this command.")
        return
        
    if mode == 'open':
        await execute_remove("✅", index - 1)
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
        await ctx.send(f"🗳️ You removed your vote to remove `#{index}`. Votes: **{current_votes}/{required_votes}**")
        return

    if current_votes >= required_votes:
        current_index = next((i for i, s in enumerate(queue) if s.get('queue_id') == target['queue_id']), None)
        if current_index is None:
            clear_votes(ctx.guild.id, action_key=action_key)
            await ctx.send("⚠️ That song is no longer in the queue.")
            return

        await execute_remove(f"✅ Vote passed (**{current_votes}/{required_votes}** of {eligible_count} listeners).", current_index)
        return

    await ctx.send(f"🗳️ Remove vote added for `#{index}` (**{current_votes}/{required_votes}** of {eligible_count} listeners).")

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

# Initialize blacklist patterns and load persistent state on startup
@bot.event
async def setup_hook():
    """Called after the bot is logged in but before on_ready."""
    global _blacklist_patterns
    try:
        validate_command_permissions_config()
        _blacklist_patterns = load_yt_blacklist_patterns()
        ensure_loop_lag_monitor()
        load_state_from_disk()
        
        # Start API server
        try:
            import api
            api.set_bot(bot)
            api.start_api_server(host=getattr(settings, "API_HOST", "0.0.0.0"), port=getattr(settings, "API_PORT", 8000))
            logger.info("API Server started successfully.")
        except Exception as api_err:
            logger.error(f"Failed to start API server: {api_err}", exc_info=True)

        logger.info(f"Initialized with {len(_blacklist_patterns)} blacklist patterns and persistent queue state.")
    except Exception as e:
        logger.error(f"Failed to initialize during setup_hook: {e}", exc_info=True)

_shutdown_lock = False

async def perform_graceful_shutdown():
    global _shutdown_lock
    if _shutdown_lock:
        return
    _shutdown_lock = True
    logger.info("Received shutdown signal, shutting down gracefully...")
    try:
        import api
        if hasattr(api, 'wait_api_server_shutdown'):
            await api.wait_api_server_shutdown(timeout=2.0)
        else:
            api.stop_api_server()
    except Exception as e:
        logger.warning(f"Error stopping API server: {e}")

    for gid, st in list(music_state_by_guild.items()):
        try:
            cancel_voice_recovery(gid)
            cancel_empty_voice_leave_timer(gid)
            st['manual_stop'] = True
            st['is_playing'] = False
            st['was_playing'] = False
            st['voice_channel_id'] = None
            st['saved_at'] = time.time()
        except Exception:
            pass

    save_state_to_disk()

    try:
        if not bot.is_closed():
            await bot.close()
    except Exception:
        pass

# Main bot startup
async def main():
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN environment variable not set. "
            "Please add it to your .env file or system environment."
        )

    logger.info("Starting MusicBot...")
    retry_delay = settings.RECONNECT_DELAY_INITIAL

    def handle_signal(sig, frame):
        logger.info(f"Signal {sig} received, triggering graceful shutdown...")
        try:
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                loop.call_soon_threadsafe(lambda: asyncio.create_task(perform_graceful_shutdown()))
        except RuntimeError:
            pass

    try:
        signal.signal(signal.SIGINT, handle_signal)
    except Exception:
        pass
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except Exception:
        pass
    if hasattr(signal, 'SIGBREAK'):
        try:
            signal.signal(signal.SIGBREAK, handle_signal)
        except Exception:
            pass

    while True:
        try:
            start_console_command_bridge()
            await bot.start(TOKEN)
            if bot.is_closed():
                break
        except (KeyboardInterrupt, asyncio.CancelledError):
            await perform_graceful_shutdown()
            break
        except Exception as e:
            if not settings.AUTO_RECONNECT:
                logger.error(f"Fatal error (auto-reconnect disabled): {e}", exc_info=True)
                raise
            logger.warning(
                f"Connection dropped or failed to connect ({e}). "
                f"Internet connection may be interrupted. Retrying in {retry_delay:.1f}s..."
            )
            try:
                if not bot.is_closed():
                    await bot.close()
            except Exception:
                pass
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, settings.RECONNECT_DELAY_MAX)
        finally:
            save_state_to_disk()

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