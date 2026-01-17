import os

# General Config
MEDIA_FOLDER = 'media'
COMMAND_PREFIX = '!'

# YouTube / YTDL Options
# Points to cookies.txt to avoid login issues
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'cookiefile': 'cookies.txt', 
    'quiet': True,
}

# FFmpeg Options
# Ensures the stream doesn't cut out on bad internet
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}