import discord
from discord.ext import commands, tasks
import os
import asyncio
import yt_dlp
import difflib
from dotenv import load_dotenv
import settings
import traceback
import logging
import socket
import subprocess
from datetime import datetime, timedelta, timezone

# Enable debug logging
logging.basicConfig(level=logging.DEBUG)

# Also log to file
file_handler = logging.FileHandler('debug.log')
file_handler.setLevel(logging.DEBUG)
logging.getLogger().addHandler(file_handler)

# Load Secrets
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# --- SETUP ---
intents = discord.Intents.default()
intents.message_content = True

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
    'options': '-vn',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(YTDL_FORMAT_OPTIONS)

# GLOBAL VARIABLES
song_queue = []
current_volume = 0.5  # Default volume (0.5 = 50%)
minecraft_server_online = False
minecraft_server_ip = None

def calculate_relative_time(date_string):
    """Calculate relative time from YYYY-MM-DD-HH-MM format (GMT+7)."""
    try:
        # Parse the date string
        date_obj = datetime.strptime(date_string, '%Y-%m-%d-%H-%M')
        
        # Get current time in GMT+7 (using timezone-aware datetime)
        gmt7 = timezone(timedelta(hours=7))
        current_time = datetime.now(timezone.utc).astimezone(gmt7).replace(tzinfo=None)
        
        # Calculate difference
        diff = current_time - date_obj
        
        # Convert to appropriate unit
        total_seconds = diff.total_seconds()
        
        if total_seconds < 60:
            return "<1 minute ago"
        elif total_seconds < 3600:  # Less than 1 hour
            minutes = int(total_seconds / 60)
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif total_seconds < 86400:  # Less than 24 hours
            hours = int(total_seconds / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:  # 24 hours or more
            days = int(total_seconds / 86400)
            return f"{days} day{'s' if days != 1 else ''} ago"
    except Exception as e:
        return "Unknown"

def load_minecraft_template():
    """Load the Minecraft message template from file."""
    template = {}
    try:
        with open('minecraft_message.txt', 'r', encoding='utf-8') as f:
            content = f.read()
            # Parse sections - find [KEY] at start of line
            import re
            pattern = r'^\[([^\]]+)\]\s*\n((?:(?!\n\[)[^\n]*\n?)*)'
            matches = re.finditer(pattern, content, re.MULTILINE)
            for match in matches:
                key = match.group(1).strip()
                value = match.group(2).strip()
                template[key] = value
    except FileNotFoundError:
        # Return default template if file doesn't exist
        template = {
            'LOADING_MESSAGE': '🔄 Fetching server information...',
            'ERROR_NO_IP': '❌ Could not fetch public IP address.',
            'ERROR_EXCEPTION': '❌ Error fetching IP: {error}',
            'EMBED_TITLE': '🎮 Minecraft Server',
            'EMBED_DESCRIPTION': '{statusEmoji} Status: **{statusText}**',
            'FIELD_1_NAME': 'Server Address',
            'FIELD_1_VALUE': '`{serverAddress}`',
            'FIELD_2_NAME': 'Map URL',
            'FIELD_2_VALUE': '[View Map]({mapUrl})',
            'FIELD_3_NAME': '📅 CLIENT LAST UPDATED',
            'FIELD_3_VALUE': '**🕒 {lastUpdated}**',
            'FIELD_4_NAME': 'Latest Client Version',
            'FIELD_4_VALUE': '**{versionName}**\n{versionDescription}',
            'FIELD_5_NAME': 'Download Client',
            'FIELD_5_VALUE': '[Google Drive Link]({driveLink})',
            'FIELD_6_NAME': 'Instructions',
            'FIELD_6_VALUE': '{instructions}',
            'VERSION_NAME': '1.20.1',
            'VERSION_DESCRIPTION': 'Forge modded client with performance mods',
            'DRIVE_LINK': 'https://drive.google.com/your-link-here',
            'INSTRUCTIONS': '1. Download the client from the link above\n2. Extract to your Minecraft folder\n3. Run the launcher and connect!',
            'LAST_UPDATED_DATE': '2026-02-12-14-30',
            'BOT_STATUS_TYPE': 'playing',
            'BOT_STATUS_TEXT': 'Type !minecraft for server info',
            'BOT_STATUS_STATE': 'online',
        }
    return template

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

async def play_next(ctx):
    """Plays the next item in the queue with volume control."""
    global current_volume
    
    if song_queue:
        song = song_queue.pop(0)
        
        try:
            print(f"🔴 DEBUG: Now playing - {song['type']}: {song['title']}")
            # 1. Create the base Source
            if song['type'] == 'local':
                source_path = os.path.join(settings.MEDIA_FOLDER, song['data'])
                source = discord.FFmpegPCMAudio(source_path)
            elif song['type'] == 'youtube':
                print(f"🔴 DEBUG: Creating FFmpeg source for YouTube")
                
                # Use discord.py's recommended approach with loop
                loop = asyncio.get_event_loop()
                data = await loop.run_in_executor(None, lambda: ytdl.extract_info(song['data'], download=False))
                
                if 'entries' in data:
                    data = data['entries'][0]
                
                filename = data['url']
                print(f"🔴 DEBUG: Stream URL obtained: {filename[:100]}")
                print(f"🔴 DEBUG: Format: {data.get('format_id')}, ext: {data.get('ext')}")
                
                source = discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS)
            elif song['type'] == 'url':
                print(f"🔴 DEBUG: Creating FFmpeg source for generic URL")
                source = discord.FFmpegPCMAudio(
                    song['data'],
                    before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                    options="-vn"
                )

            # 2. Apply Volume Transformer
            # This wrapper allows us to change volume
            source = discord.PCMVolumeTransformer(source)
            source.volume = current_volume

            # 3. Play
            ctx.voice_client.play(
                source, 
                after=lambda e: bot.loop.create_task(play_next(ctx))
            )
            
            await ctx.send(f"🎶 **Now Playing:** {song['title']} (Vol: {int(current_volume * 100)}%)")
        
        except Exception as e:
            print(f"🔴 DEBUG ERROR in play_next:")
            print(traceback.format_exc())
            await ctx.send(f"❌ Error playing next song: {e}")
            await play_next(ctx) # Try the next one
    else:
        pass

# --- EVENTS ---

async def set_bot_status():
    """Set bot status from template."""
    template = load_minecraft_template()
    status_type = template.get('BOT_STATUS_TYPE', 'playing').lower()
    status_text = template.get('BOT_STATUS_TEXT', 'Type !help for commands')
    status_state = template.get('BOT_STATUS_STATE', 'online').lower()
    
    # Map activity types
    if status_type == 'playing':
        activity = discord.Game(name=status_text)
    elif status_type == 'listening':
        activity = discord.Activity(type=discord.ActivityType.listening, name=status_text)
    elif status_type == 'watching':
        activity = discord.Activity(type=discord.ActivityType.watching, name=status_text)
    elif status_type == 'streaming':
        activity = discord.Streaming(name=status_text, url="https://twitch.tv/placeholder")
    else:
        activity = discord.Game(name=status_text)
    
    # Map status states
    if status_state == 'online':
        discord_status = discord.Status.online
    elif status_state == 'idle':
        discord_status = discord.Status.idle
    elif status_state == 'dnd' or status_state == 'do_not_disturb':
        discord_status = discord.Status.dnd
    elif status_state == 'invisible':
        discord_status = discord.Status.invisible
    else:
        discord_status = discord.Status.online
    
    await bot.change_presence(activity=activity, status=discord_status)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    print('Administrator mode: OFF')
    await set_bot_status()
    print('Bot status set from template')

@bot.event
async def on_command_error(ctx, error):
    """Handles permission errors nicely."""
    if isinstance(error, commands.CommandNotFound):
        pass # Ignore invalid commands
    else:
        await ctx.send(f"❌ An error occurred: {error}")

# --- COMMANDS ---

@bot.command()
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        await channel.connect()
        await ctx.send(f"👋 Joined **{channel}**")
    else:
        await ctx.send("You need to be in a voice channel first.")

@bot.command()
async def play(ctx, *, query):
    """Plays LOCAL file."""
    if not ctx.voice_client:
        if ctx.author.voice: await ctx.author.voice.channel.connect()
        else: return await ctx.send("Join a voice channel first.")

    filename = find_best_match(query)
    if not filename:
        return await ctx.send(f"❌ File not found matching: {query}")

    song_obj = {'type': 'local', 'title': filename, 'data': filename}
    song_queue.append(song_obj)

    if not ctx.voice_client.is_playing():
        await play_next(ctx)
    else:
        await ctx.send(f"✅ Added to queue: `{filename}`")

@bot.command()
async def yt(ctx, *, query):
    """Plays from YouTube (Url or Search)."""
    if not ctx.voice_client:
        if ctx.author.voice: await ctx.author.voice.channel.connect()
        else: return await ctx.send("Join a voice channel first.")

    # 1. Determine if the user provided a LINK or a SEARCH PHRASE
    # We check if it starts with http or www
    if query.startswith(("http://", "https://", "www.")):
        search_query = query
        await ctx.send(f"🔗 Loading Link...")
    else:
        search_query = f"ytsearch:{query}"
        await ctx.send(f"🔎 Searching YouTube for: **{query}**...")

    try:
        data = ytdl.extract_info(search_query, download=False)
        
        # 3. Handle Search Results vs Direct Links
        if 'entries' in data:
            video_data = data['entries'][0]
        else:
            video_data = data
        
        title = video_data['title']
        # Use webpage_url - this will be processed by yt-dlp again during playback
        webpage_url = video_data.get('webpage_url') or video_data.get('url')
        
        if not webpage_url:
            print(f"🔴 DEBUG: No URL found. Available keys: {video_data.keys()}")
            return await ctx.send("❌ Error: Could not extract URL from video.")
        
        print(f"🔴 DEBUG: Using webpage URL: {webpage_url}")
        
        song_obj = {'type': 'youtube', 'title': title, 'data': webpage_url}
        song_queue.append(song_obj)
        print(f"🔴 DEBUG: Added song to queue - Title: {title}")

        if not ctx.voice_client.is_playing():
            await play_next(ctx)
        else:
            await ctx.send(f"✅ Added to queue: `{title}`")

    except Exception as e:
        print(f"🔴 DEBUG ERROR in yt command:")
        print(traceback.format_exc())
        await ctx.send(f"❌ Error: {e}")

@bot.command()
async def volume(ctx, volume: int):
    """Sets volume (0-100)."""
    global current_volume
    
    # Clamp value between 0 and 100
    if volume < 0: volume = 0
    if volume > 100: volume = 100

    # Convert to float (0.0 - 1.0)
    current_volume = volume / 100

    # Adjust currently playing song immediately
    if ctx.voice_client and ctx.voice_client.source:
        ctx.voice_client.source.volume = current_volume

    await ctx.send(f"🔊 Volume set to **{volume}%**")

@bot.command()

async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped.")

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

    # Discord has a message limit of 2000 chars. 
    # If queue is huge, just show the first 10.
    if len(queue_list) > 1900:
        await ctx.send(f"{queue_list[:1900]}...\n*(and more)*")
    else:
        await ctx.send(queue_list)

@bot.command()
async def skipto(ctx, index: int):
    """Skips to a specific number in the queue."""
    global song_queue
    
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("Nothing is playing right now.")
        return

    if not song_queue:
        await ctx.send("The queue is empty.")
        return

    # Validate the number
    if index < 1 or index > len(song_queue):
        await ctx.send(f"Invalid number. Please choose between 1 and {len(song_queue)}.")
        return

    # Logic: Slice the queue to remove everything BEFORE the target
    # index-1 because users see 1-based, list is 0-based
    song_queue = song_queue[index-1:]

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
    song_queue.clear()
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send("🛑 Stopped.")

@bot.command()
async def minecraft(ctx):
    """Returns Minecraft server connection info."""
    global minecraft_server_ip, minecraft_server_online
    
    # Load template
    template = load_minecraft_template()
    
    if minecraft_server_ip is None:
        await ctx.send(template['LOADING_MESSAGE'])
        # Try to get IP
        try:
            result = subprocess.run(
                ['curl', 'ipv4.icanhazip.com'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                minecraft_server_ip = result.stdout.strip()
            else:
                return await ctx.send(template['ERROR_NO_IP'])
        except Exception as e:
            error_msg = template['ERROR_EXCEPTION'].replace('{error}', str(e))
            return await ctx.send(error_msg)
    
    # Check if server is online by attempting socket connection
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        # Try to connect to localhost on port 25565
        result = sock.connect_ex((minecraft_server_ip, 25565))
        sock.close()
        minecraft_server_online = (result == 0)
    except Exception as e:
        print(f"❌ Error checking Minecraft server: {e}")
        minecraft_server_online = False
    
    # Prepare variables
    server_address = f"{minecraft_server_ip}:25565"
    map_url = f"http://{minecraft_server_ip}:8080"
    status_emoji = "🟢" if minecraft_server_online else "🔴"
    status_text = "Online" if minecraft_server_online else "Offline"
    
    # Calculate last updated time
    last_updated_date = template.get('LAST_UPDATED_DATE', '')
    last_updated_text = calculate_relative_time(last_updated_date) if last_updated_date else 'Unknown'
    
    # Replace variables in template
    variables = {
        '{serverAddress}': server_address,
        '{mapUrl}': map_url,
        '{statusEmoji}': status_emoji,
        '{statusText}': status_text,
        '{lastUpdated}': last_updated_text,
        '{versionName}': template.get('VERSION_NAME', 'N/A'),
        '{versionDescription}': template.get('VERSION_DESCRIPTION', ''),
        '{driveLink}': template.get('DRIVE_LINK', 'https://drive.google.com'),
        '{instructions}': template.get('INSTRUCTIONS', 'No instructions provided'),
    }
    
    def replace_vars(text):
        for var, value in variables.items():
            text = text.replace(var, value)
        return text
    
    # Build embed from template
    embed = discord.Embed(
        title=replace_vars(template['EMBED_TITLE']),
        description=replace_vars(template['EMBED_DESCRIPTION']),
        color=discord.Color.green() if minecraft_server_online else discord.Color.red()
    )
    embed.add_field(
        name=replace_vars(template['FIELD_1_NAME']),
        value=replace_vars(template['FIELD_1_VALUE']),
        inline=False
    )
    embed.add_field(
        name=replace_vars(template['FIELD_2_NAME']),
        value=replace_vars(template['FIELD_2_VALUE']),
        inline=False
    )
    embed.add_field(
        name=replace_vars(template['FIELD_3_NAME']),
        value=replace_vars(template['FIELD_3_VALUE']),
        inline=False
    )
    embed.add_field(
        name=replace_vars(template['FIELD_4_NAME']),
        value=replace_vars(template['FIELD_4_VALUE']),
        inline=False
    )
    embed.add_field(
        name=replace_vars(template['FIELD_5_NAME']),
        value=replace_vars(template['FIELD_5_VALUE']),
        inline=False
    )
    embed.add_field(
        name=replace_vars(template['FIELD_6_NAME']),
        value=replace_vars(template['FIELD_6_VALUE']),
        inline=False
    )
    
    await ctx.send(embed=embed)

bot.run(TOKEN)