import discord
from discord.ext import commands
import os
import asyncio
import yt_dlp
import difflib
from dotenv import load_dotenv
import settings

# Load Secrets
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# --- SETUP ---
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=settings.COMMAND_PREFIX, intents=intents)

# GLOBAL VARIABLES
song_queue = []
current_volume = 1  # Default volume (0.5 = 50%)

# --- HELPER FUNCTIONS ---

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
            # 1. Create the base Source
            if song['type'] == 'local':
                source_path = os.path.join(settings.MEDIA_FOLDER, song['data'])
                source = discord.FFmpegPCMAudio(source_path)
            elif song['type'] == 'url':
                source = discord.FFmpegPCMAudio(song['data'], **settings.FFMPEG_OPTIONS)

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
            await ctx.send(f"❌ Error playing next song: {e}")
            await play_next(ctx) # Try the next one
    else:
        pass

# --- EVENTS ---

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    print('Administrator mode: ON')

@bot.event
async def on_command_error(ctx, error):
    """Handles permission errors nicely."""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 **Access Denied:** You need `Administrator` permission to use this bot.")
    elif isinstance(error, commands.CommandNotFound):
        pass # Ignore invalid commands
    else:
        await ctx.send(f"❌ An error occurred: {error}")

# --- COMMANDS ---

@bot.command()
@commands.has_permissions(administrator=True)
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        await channel.connect()
        await ctx.send(f"👋 Joined **{channel}**")
    else:
        await ctx.send("You need to be in a voice channel first.")

@bot.command()
@commands.has_permissions(administrator=True)
async def play(ctx, *, query):
    """Plays LOCAL file (Admin Only)."""
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
@commands.has_permissions(administrator=True)
async def yt(ctx, *, query):
    """Plays from YOUTUBE (Admin Only)."""
    if not ctx.voice_client:
        if ctx.author.voice: await ctx.author.voice.channel.connect()
        else: return await ctx.send("Join a voice channel first.")

    await ctx.send(f"🔎 Searching YouTube for: **{query}**...")

    try:
        with yt_dlp.YoutubeDL(settings.YTDL_OPTIONS) as ydl:
            info = ydl.extract_info(f"ytsearch:{query}", download=False)
            if 'entries' in info: info = info['entries'][0]
            
            url = info['url']
            title = info['title']
            
            song_obj = {'type': 'url', 'title': title, 'data': url}
            song_queue.append(song_obj)

            if not ctx.voice_client.is_playing():
                await play_next(ctx)
            else:
                await ctx.send(f"✅ Added to queue: `{title}`")
    except Exception as e:
        await ctx.send(f"Error: {e}")

@bot.command()
@commands.has_permissions(administrator=True)
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
@commands.has_permissions(administrator=True)
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped.")

@bot.command()
@commands.has_permissions(administrator=True)
async def stop(ctx):
    song_queue.clear()
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send("🛑 Stopped.")

bot.run(TOKEN)