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

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    print('Administrator mode: OFF')
    check_minecraft_server.start()  # Start background task
    print('Minecraft server monitoring started')

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
    
    if minecraft_server_ip is None:
        await ctx.send("🔄 Fetching server information...")
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
                return await ctx.send("❌ Could not fetch public IP address.")
        except Exception as e:
            return await ctx.send(f"❌ Error fetching IP: {e}")
    
    server_address = f"{minecraft_server_ip}:25565"
    map_url = f"http://{minecraft_server_ip}:8080"
    status_emoji = "🟢" if minecraft_server_online else "🔴"
    status_text = "Online" if minecraft_server_online else "Offline"
    
    embed = discord.Embed(
        title="🎮 Minecraft Server",
        description=f"{status_emoji} Status: **{status_text}**",
        color=discord.Color.green() if minecraft_server_online else discord.Color.red()
    )
    embed.add_field(name="Server Address", value=f"`{server_address}`", inline=False)
    embed.add_field(name="Map URL", value=f"[View Map]({map_url})", inline=False)
    embed.add_field(name="Connect", value=f"Use this address in Minecraft to join!", inline=False)
    
    await ctx.send(embed=embed)

# Background task to check Minecraft server status
@tasks.loop(seconds=60)
async def check_minecraft_server():
    """Checks if Minecraft server is online and updates bot status."""
    global minecraft_server_online, minecraft_server_ip
    
    # Get public IP if not already cached
    if minecraft_server_ip is None:
        try:
            result = subprocess.run(
                ['curl', 'ipv4.icanhazip.com'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                minecraft_server_ip = result.stdout.strip()
                print(f"📡 Detected public IP: {minecraft_server_ip}")
        except Exception as e:
            print(f"❌ Error fetching public IP: {e}")
            minecraft_server_ip = "localhost"  # Fallback
    
    # Check if server is online by attempting socket connection
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        # Try to connect to localhost on port 25565
        result = sock.connect_ex(('localhost', 25565))
        sock.close()
        
        was_online = minecraft_server_online
        minecraft_server_online = (result == 0)
        
        # Update bot status
        if minecraft_server_online:
            await bot.change_presence(
                activity=discord.Game(name="Minecraft: Online 🟢"),
                status=discord.Status.online
            )
            if not was_online:
                print("✅ Minecraft server is now ONLINE")
        else:
            await bot.change_presence(
                activity=discord.Game(name="Minecraft: Offline 🔴"),
                status=discord.Status.idle
            )
            if was_online:
                print("⚠️ Minecraft server is now OFFLINE")
    
    except Exception as e:
        print(f"❌ Error checking Minecraft server: {e}")
        minecraft_server_online = False
        await bot.change_presence(
            activity=discord.Game(name="Minecraft: Unknown ⚪"),
            status=discord.Status.idle
        )

@check_minecraft_server.before_loop
async def before_check_server():
    """Wait for bot to be ready before starting the loop."""
    await bot.wait_until_ready()

bot.run(TOKEN)