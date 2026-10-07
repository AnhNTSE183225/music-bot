import os
import asyncio
import logging
from uuid import uuid4
import urllib.parse

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
import httpx
import sys
bot_module = sys.modules.get('__main__')
import settings

logger = logging.getLogger(__name__)

app = FastAPI(title="MusicBot API")

# We can directly access bot_module.bot instead of bot_instance now
bot_instance = None

def set_bot(bot):
    global bot_instance
    bot_instance = bot

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = getattr(settings, "DISCORD_REDIRECT_URI", "http://localhost:8000/callback")
FRONTEND_URL = getattr(settings, "FRONTEND_URL", "http://localhost:3000")

frontend_origin = "http://localhost:3000"
if FRONTEND_URL:
    try:
        parsed = urllib.parse.urlparse(FRONTEND_URL)
        frontend_origin = f"{parsed.scheme}://{parsed.netloc}"
    except:
        pass

app.add_middleware(
    CORSMiddleware,
    allow_origins=[frontend_origin, "http://localhost:3000", "http://127.0.0.1:3000", "https://anhntse183225.github.io"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSIONS = {}

class AddSongRequest(BaseModel):
    url: str

class VolumeRequest(BaseModel):
    volume: int

@app.get("/login")
def login():
    if not DISCORD_CLIENT_ID:
        raise HTTPException(status_code=500, detail="DISCORD_CLIENT_ID not configured")
    
    url = "https://discord.com/api/oauth2/authorize"
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds"
    }
    redirect_url = f"{url}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(redirect_url)

@app.get("/callback")
async def callback(code: str, response: Response):
    if not code:
        raise HTTPException(status_code=400, detail="No code provided")
    
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    
    async with httpx.AsyncClient() as client:
        token_res = await client.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
        if token_res.status_code != 200:
            logger.error(f"Failed to get token: {token_res.text}")
            raise HTTPException(status_code=400, detail="Authentication failed")
            
        token_data = token_res.json()
        access_token = token_data.get("access_token")
        
        user_res = await client.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"})
        user_data = user_res.json()
        
        guilds_res = await client.get("https://discord.com/api/users/@me/guilds", headers={"Authorization": f"Bearer {access_token}"})
        guilds_data = guilds_res.json()
        
        session_id = str(uuid4())
        SESSIONS[session_id] = {
            "user": user_data,
            "guilds": guilds_data,
            "access_token": access_token
        }
        
        separator = "&" if "?" in FRONTEND_URL else "?"
        res = RedirectResponse(url=f"{FRONTEND_URL}{separator}token={session_id}")
        return res

from fastapi.security import OAuth2PasswordBearer
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    if not token or token not in SESSIONS:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return SESSIONS[token]

@app.get("/api/me")
async def get_me(user: dict = Depends(get_current_user)):
    return {"user": user["user"]}

@app.get("/api/voice_status")
async def get_voice_status(user: dict = Depends(get_current_user)):
    user_id = int(user["user"]["id"])
    active_sessions = []
    
    logger.info(f"Checking voice status for user {user_id}")
    for guild in bot_instance.guilds:
        member = guild.get_member(user_id)
        if member:
            logger.info(f"Found member in guild {guild.name}. Voice state: {member.voice}")
            if member.voice and member.voice.channel:
                bot_in_channel = guild.voice_client is not None and guild.voice_client.channel.id == member.voice.channel.id
                
                icon_url = None
                if getattr(guild, 'icon', None):
                    icon_url = str(guild.icon.url)
                    
                is_admin = bot_module.is_admin_member(member)
                active_sessions.append({
                    "guild_id": str(guild.id),
                    "guild_name": guild.name,
                    "guild_icon": icon_url,
                    "channel_id": str(member.voice.channel.id),
                    "channel_name": member.voice.channel.name,
                    "bot_connected": bot_in_channel,
                    "is_admin": is_admin
                })
        else:
            # Maybe the member isn't in cache? Let's check voice channels manually.
            for vc in guild.voice_channels:
                for vc_member in vc.members:
                    if vc_member.id == user_id:
                        logger.info(f"Found user {user_id} in voice channel {vc.name} manually!")
                        bot_in_channel = guild.voice_client is not None and guild.voice_client.channel.id == vc.id
                        icon_url = str(guild.icon.url) if getattr(guild, 'icon', None) else None
                        is_admin = bot_module.is_admin_member(member)
                        active_sessions.append({
                            "guild_id": str(guild.id),
                            "guild_name": guild.name,
                            "guild_icon": icon_url,
                            "channel_id": str(vc.id),
                            "channel_name": vc.name,
                            "bot_connected": bot_in_channel,
                            "is_admin": is_admin
                        })
            
    return {"sessions": active_sessions}

async def verify_guild_access(guild_id: str, user: dict):
    user_guilds = [str(g["id"]) for g in user["guilds"]]
    if str(guild_id) not in user_guilds:
        raise HTTPException(status_code=403, detail="You are not in this server")
        
    guild = bot_instance.get_guild(int(guild_id))
    if not guild:
        raise HTTPException(status_code=404, detail="Bot is not in this server")
        
    return guild

def get_simulated_context(guild, user_data):
    # Fetch member from guild to act as context author
    user_id = int(user_data["user"]["id"])
    member = guild.get_member(user_id)
    if not member:
        # Fallback author using ConsoleAuthor
        member = bot_module.create_console_author()
        member.id = user_id
        member.name = user_data["user"]["username"]
        member.display_name = user_data["user"]["username"]

    # Use a generic channel or text channel from state
    state = bot_module.get_music_state(guild)
    text_channel = guild.get_channel(state.get("text_channel_id")) if state else None
    
    ctx = bot_module.MusicContext(bot=bot_instance, guild=guild, channel=text_channel, author=member)
    return ctx

@app.get("/api/queue/{guild_id}")
async def get_queue(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    
    state = bot_module.get_music_state(guild)
    if not state:
        return {"queue": [], "current": None, "volume": 100, "loop": False, "is_playing": False}
        
    queue = state.get("queue", [])
    current = state.get("current_song")
    
    # We need to return serializable objects. `current` might have datetime objects or other non-serializable fields,
    # but based on save_state_to_disk it's mostly a dict of strings/ints.
    
    vc = guild.voice_client if hasattr(guild, 'voice_client') else None
    is_paused = vc.is_paused() if vc else False
    
    position = 0
    if vc and vc.source:
        try:
            # vc.source is typically a PCMVolumeTransformer, whose original source is LoggingFFmpegPCMAudio
            original = vc.source.original if hasattr(vc.source, 'original') else vc.source
            if hasattr(original, 'frames_read'):
                position = original.frames_read * 0.02
        except Exception:
            pass

    votes_info = bot_module.get_skip_votes_info(guild) if hasattr(bot_module, 'get_skip_votes_info') else (0, 0, 0)
    current_votes, required_votes, eligible_count = votes_info

    return {
        "queue": queue,
        "current": current,
        "queue_index": state.get("queue_index", -1),
        "volume": state.get("volume", 100),
        "loop": state.get("loop_enabled", False),
        "is_playing": state.get("is_playing", False),
        "is_paused": is_paused,
        "position": position,
        "skip_votes": current_votes,
        "skip_votes_required": required_votes
    }

@app.post("/api/queue/{guild_id}")
async def add_song(guild_id: str, req: AddSongRequest, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    yt_command = bot_instance.get_command('yt')
    if not yt_command:
        raise HTTPException(status_code=500, detail="yt command not found in bot")
        
    # Execute the yt command callback
    # We need to run it safely
    try:
        await yt_command.callback(ctx, query=req.url)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error adding song: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/controls/{guild_id}/skip")
async def skip_song(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    cmd = bot_instance.get_command('skip')
    try:
        await cmd.callback(ctx)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class IndexRequest(BaseModel):
    index: int

@app.post("/api/controls/{guild_id}/skipto")
async def skipto_song(guild_id: str, req: IndexRequest, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    cmd = bot_instance.get_command('skipto')
    try:
        await cmd.callback(ctx, req.index)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/controls/{guild_id}/remove")
async def remove_song(guild_id: str, req: IndexRequest, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    cmd = bot_instance.get_command('remove')
    try:
        await cmd.callback(ctx, req.index)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/controls/{guild_id}/clear")
async def clear_queue(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    cmd = bot_instance.get_command('clear')
    try:
        await cmd.callback(ctx)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/controls/{guild_id}/join")
async def join_channel(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    cmd = bot_instance.get_command('join')
    try:
        await cmd.callback(ctx)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/controls/{guild_id}/stop")
async def stop_song(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    cmd = bot_instance.get_command('stop')
    try:
        await cmd.callback(ctx)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/controls/{guild_id}/pause")
async def pause_song(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    cmd = bot_instance.get_command('pause')
    try:
        await cmd.callback(ctx)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/controls/{guild_id}/resume")
async def resume_song(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    cmd = bot_instance.get_command('resume')
    try:
        await cmd.callback(ctx)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/controls/{guild_id}/loop")
async def loop_song(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    cmd = bot_instance.get_command('loop')
    try:
        await cmd.callback(ctx)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/controls/{guild_id}/volume")
async def change_volume(guild_id: str, req: VolumeRequest, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    
    cmd = bot_instance.get_command('volume')
    try:
        await cmd.callback(ctx, volume=req.volume)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def start_api_server(host="0.0.0.0", port=8000):
    import uvicorn
    # Pass log_config=None so Uvicorn uses the bot's existing UTF-8 logging setup
    # instead of closing sys.stdout and overwriting existing loggers.
    config = uvicorn.Config(app, host=host, port=port, log_config=None)
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
