import os

# General Config
MEDIA_FOLDER = 'media'
COMMAND_PREFIX = '!'

# YouTube / YTDL Options
# Points to cookies.txt to avoid login issues
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    # 'cookiefile': 'cookies.txt', 
    'quiet': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
}

# FFmpeg Options
# Ensures the stream doesn't cut out on bad internet
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}