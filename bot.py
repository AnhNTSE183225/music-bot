import discord
from discord.ext import commands, tasks
import os
import asyncio
import yt_dlp
import difflib
import re
from dotenv import load_dotenv
import settings
import traceback
import logging
import importlib
from datetime import datetime, timedelta, timezone

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
TOKEN = os.getenv('DISCORD_TOKEN')

# Validate token on startup
if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN environment variable not set. "
        "Please add it to your .env file or system environment."
    )

# --- SETUP ---
intents = discord.Intents.all()

bot = commands.Bot(command_prefix=settings.COMMAND_PREFIX, intents=intents)

# YouTube / YTDL Options
YTDL_FORMAT_OPTIONS = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extractor_args': {'youtube': {'player_client': ['android']}},
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)

# GLOBAL VARIABLES
song_queue = []
current_volume = settings.DEFAULT_VOLUME
play_next_lock = asyncio.Lock()

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

async def ensure_voice_connected(ctx):
    """Ensure bot is connected to user's voice channel. Returns True on success."""
    if ctx.voice_client and ctx.voice_client.is_connected():
        return True
    
    if not ctx.author.voice:
        await ctx.send("❌ You must be in a voice channel first.")
        return False
    
    try:
        await ctx.author.voice.channel.connect(timeout=10.0, reconnect=True)
        await asyncio.sleep(0.5)
        return True
    except Exception as e:
        logger.error(f"Failed to connect to voice channel: {e}")
        await ctx.send(f"❌ Failed to connect: {e}")
        return False

async def play_next(ctx):
    """Plays the next item in the queue with volume control. Must be called within play_next_lock."""
    global current_volume

    # Check if voice client exists and is connected
    if not ctx.voice_client or not ctx.voice_client.is_connected():
        logger.debug("Voice client not connected, cannot play next song")
        return

    # Guard against concurrent starters.
    if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
        return

    if not song_queue:
        return

    song = song_queue.pop(0)

    try:
        logger.debug(f"Now playing - {song['type']}: {song['title']}")
        # 1. Create the base Source
        if song['type'] == 'local':
            source_path = os.path.join(settings.MEDIA_FOLDER, song['data'])
            source = discord.FFmpegPCMAudio(source_path)
        elif song['type'] == 'youtube':
            logger.debug(f"Creating FFmpeg source for YouTube: {song['data']}")

            # Use discord.py's recommended approach with executor (non-blocking)
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(song['data'], download=False))

            if 'entries' in data:
                data = data['entries'][0]

            filename = data['url']
            logger.debug(f"Stream URL obtained: {filename[:100]}...")
            logger.debug(f"Format: {data.get('format_id')}, ext: {data.get('ext')}")

            source = discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS)
        elif song['type'] == 'url':
            logger.debug(f"Creating FFmpeg source for generic URL: {song['data']}")
            source = discord.FFmpegPCMAudio(
                song['data'],
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn"
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

            def after_playback(error):
                """Called after playback ends. Schedules next song with proper lock protection."""
                if error:
                    logger.error(f"Playback callback error: {error}")
                # Schedule play_next with lock to prevent race conditions
                async def next_with_lock():
                    async with play_next_lock:
                        if ctx.voice_client and ctx.voice_client.is_connected():
                            await play_next(ctx)
                bot.loop.call_soon_threadsafe(lambda: bot.loop.create_task(next_with_lock()))

            ctx.voice_client.play(source, after=after_playback)
            await ctx.send(f"🎶 **Now Playing:** {song['title']} (Vol: {int(current_volume * 100)}%)")
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
    print(f'Logged in as {bot.user}')
    print('Administrator mode: OFF')

@bot.event
async def on_command_error(ctx, error):
    """Handles permission errors nicely."""
    if isinstance(error, commands.CommandNotFound):
        pass  # Ignore invalid commands
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument provided. {error}")
    else:
        logger.error(f"Command error in {ctx.command}: {error}", exc_info=True)
        await ctx.send(f"❌ An error occurred: {error}")

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
            await channel.connect(timeout=10.0, reconnect=True)
            await asyncio.sleep(0.5)
            await ctx.send(f"👋 Joined **{channel}**")
    else:
        await ctx.send("❌ You need to be in a voice channel first.")

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

    song_obj = {'type': 'local', 'title': filename, 'data': filename}
    song_queue.append(song_obj)

    async with play_next_lock:
        if not ctx.voice_client.is_playing():
            await play_next(ctx)
        else:
            await ctx.send(f"✅ Added to queue: `{filename}`")

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
        # Run blocking yt-dlp call in executor to avoid freezing the event loop
        data = await asyncio.to_thread(ytdl.extract_info, search_query, False)
        
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
        
        song_obj = {'type': 'youtube', 'title': title, 'data': webpage_url}
        song_queue.append(song_obj)
        logger.debug(f"Added song to queue - Title: {title}")

        # Double-check voice connection before playing
        if not ctx.voice_client:
            return await ctx.send("❌ Lost voice connection.")

        async with play_next_lock:
            if not ctx.voice_client.is_playing():
                await play_next(ctx)
            else:
                await ctx.send(f"✅ Added to queue: `{title}`")

    except Exception as e:
        logger.error(f"Error in yt command: {e}", exc_info=True)
        await ctx.send(f"❌ Error: {e}")

@bot.command()
async def volume(ctx, volume: int):
    """Sets volume (0-100). Usage: !volume <0-100>"""
    global current_volume
    
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
    """Skips the current song."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped.")
    else:
        await ctx.send("❌ Nothing is playing.")

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
        queue_list += f"`{i+1}.` {song['title']}\n"

    # Discord has a message limit; if queue is huge, show first N
    max_chars = settings.DISCORD_MESSAGE_CHAR_LIMIT - settings.MESSAGE_BUFFER
    if len(queue_list) > max_chars:
        await ctx.send(f"{queue_list[:max_chars]}...\n*(and more)*")
    else:
        await ctx.send(queue_list)

@bot.command()
async def skipto(ctx, index: int):
    """Skips to a specific number in the queue. Usage: !skipto <position>"""
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
    song_queue.clear()
    await ctx.send("🗑️ **Queue cleared.**")

@bot.command()
async def stop(ctx):
    """Stops playback and disconnects from voice channel."""
    song_queue.clear()
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send("🛑 Stopped and disconnected.")
    else:
        await ctx.send("❌ Not connected to a voice channel.")



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
        _blacklist_patterns = load_yt_blacklist_patterns()
        logger.info(f"Blacklist initialized with {len(_blacklist_patterns)} patterns.")
    except Exception as e:
        logger.error(f"Failed to initialize blacklist: {e}", exc_info=True)

# Main bot startup
if __name__ == '__main__':
    logger.info("Starting MusicBot...")
    print("Starting MusicBot...")
    bot.run(TOKEN)