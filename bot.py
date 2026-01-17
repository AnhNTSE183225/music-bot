import discord
from discord.ext import commands
import os
import asyncio
import yt_dlp
import difflib
from dotenv import load_dotenv

# Import settings from our separate file
import settings

# Load the .env file
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

# Setup Intent
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=settings.COMMAND_PREFIX, intents=intents)

# UNIFIED QUEUE
# We store dictionaries: {'type': 'local' or 'url', 'title': 'song name', 'data': 'filename or url'}
song_queue = []

def find_best_match(query):
    """Smart search for local files."""
    if not os.path.exists(settings.MEDIA_FOLDER):
        os.makedirs(settings.MEDIA_FOLDER)
        return None

    files = [f for f in os.listdir(settings.MEDIA_FOLDER) if f.endswith(('.mp3', '.mp4'))]
    query = query.lower()

    # 1. Exact / 2. Partial
    for f in files:
        if query == f.lower() or query in f.lower():
            return f
    # 3. Fuzzy
    close_matches = difflib.get_close_matches(query, files, n=1, cutoff=0.5)
    return close_matches[0] if close_matches else None

async def play_next(ctx):
    """Plays the next item in the queue."""
    if song_queue:
        song = song_queue.pop(0)
        
        # Determine source based on type
        if song['type'] == 'local':
            source_path = os.path.join(settings.MEDIA_FOLDER, song['data'])
            source = discord.FFmpegPCMAudio(source_path)
        
        elif song['type'] == 'url':
            source = discord.FFmpegPCMAudio(song['data'], **settings.FFMPEG_OPTIONS)

        # Play and setup callback
        # Note: We must pass a standard function to 'after', so we wrap the async function
        ctx.voice_client.play(
            source, 
            after=lambda e: bot.loop.create_task(play_next(ctx))
        )
        
        await ctx.send(f"🎶 **Now Playing:** {song['title']}")
    else:
        # Queue finished
        pass

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    print(f'Media Folder: {settings.MEDIA_FOLDER}')

@bot.command()
async def join(ctx):
    """Joins the voice channel."""
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        await channel.connect()
        await ctx.send(f"👋 Joined **{channel}**")
    else:
        await ctx.send("You need to be in a voice channel first.")

@bot.command()
async def play(ctx, *, query):
    """Plays a LOCAL file (Smart Search)."""
    if not ctx.voice_client:
        if ctx.author.voice: await ctx.author.voice.channel.connect()
        else: return await ctx.send("Join a voice channel first.")

    filename = find_best_match(query)
    if not filename:
        return await ctx.send(f"❌ File not found matching: {query}")

    # Add to queue object
    song_obj = {'type': 'local', 'title': filename, 'data': filename}
    song_queue.append(song_obj)

    if not ctx.voice_client.is_playing():
        await play_next(ctx)
    else:
        await ctx.send(f"✅ Added to queue: `{filename}`")

@bot.command()
async def yt(ctx, *, query):
    """Plays from YOUTUBE (Search)."""
    if not ctx.voice_client:
        if ctx.author.voice: await ctx.author.voice.channel.connect()
        else: return await ctx.send("Join a voice channel first.")

    await ctx.send(f"🔎 Searching YouTube for: **{query}**...")

    # Extract Info
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
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop() # Triggers play_next
        await ctx.send("⏭️ Skipped.")

@bot.command()
async def stop(ctx):
    song_queue.clear()
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send("🛑 Stopped.")

bot.run(TOKEN)