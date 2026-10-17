import os
import asyncio
import logging
from uuid import uuid4
import urllib.parse

from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse, PlainTextResponse, FileResponse
import json
from pydantic import BaseModel
import httpx
import sys
bot_module = sys.modules.get('__main__')
import settings
import lyrics_service
import ai_service
import db

# Initialize database schema
db.init_db()

logger = logging.getLogger(__name__)

app = FastAPI(title="MusicBot API")

is_shutting_down = False

@app.on_event("shutdown")
def shutdown_event():
    global is_shutting_down
    is_shutting_down = True

# We can directly access bot_module.bot instead of bot_instance now
bot_instance = None

def set_bot(bot):
    global bot_instance
    bot_instance = bot

DISCORD_CLIENT_ID = (getattr(settings, "DISCORD_CLIENT_ID", None) or os.getenv("DEV_DISCORD_CLIENT_ID") or os.getenv("DISCORD_CLIENT_ID", "")).strip()
DISCORD_CLIENT_SECRET = (getattr(settings, "DISCORD_CLIENT_SECRET", None) or os.getenv("DEV_DISCORD_CLIENT_SECRET") or os.getenv("DISCORD_CLIENT_SECRET", "")).strip()
DISCORD_REDIRECT_URI = (getattr(settings, "DISCORD_REDIRECT_URI", "http://localhost:8000/callback")).strip()
FRONTEND_URL = (getattr(settings, "FRONTEND_URL", "http://localhost:3000")).strip()

frontend_origin = ""
if FRONTEND_URL:
    try:
        parsed = urllib.parse.urlparse(FRONTEND_URL)
        frontend_origin = f"{parsed.scheme}://{parsed.netloc}"
    except:
        pass

cors_origins = list(getattr(settings, "CORS_ORIGINS", []) or [])
if frontend_origin and frontend_origin not in cors_origins:
    cors_origins.append(frontend_origin)
if not cors_origins:
    cors_origins = ["http://localhost:3000", "http://127.0.0.1:3000", "http://localhost:8080", "http://127.0.0.1:8080"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AddSongRequest(BaseModel):
    url: str
    title: str | None = None
    artist: str | None = None
    thumbnail: str | None = None
    duration: float | None = None

class AIBatchConfirmRequest(BaseModel):
    songs: list[AddSongRequest]

class VolumeRequest(BaseModel):
    volume: int

class IndexRequest(BaseModel):
    index: int

class ContributeKeyRequest(BaseModel):
    api_key: str

# ---------------------------------------------------------------------------
# AI Auto-Queue — models and in-memory state
# ---------------------------------------------------------------------------

class AISearchRequest(BaseModel):
    prompt: str
    count: int = 5        # how many songs to suggest / add per fill
    threshold: int = 2    # auto-fill trigger: when queue length falls below this

class AIAutoQueueConfig(BaseModel):
    enabled: bool
    prompt: str
    count: int = 5
    threshold: int = 2
    set_by_name: str | None = None
    set_by_avatar: str | None = None

# guild_id -> {enabled, prompt, count, threshold, set_by_name, set_by_avatar}
# Load saved configurations from SQLite; all initialized as enabled=False
try:
    _ai_autoqueue_state: dict[str, dict] = db.load_all_autoqueue_configs()
except Exception as _load_exc:
    logger.warning("[AI AutoQueue] Could not load persisted configs from SQLite: %s", _load_exc)
    _ai_autoqueue_state = {}


def disable_autoqueue(guild_id: str | int | None) -> None:
    """Disable auto-queue toggle for a specific guild (e.g. when leaving voice or guild), keeping the vibe config intact."""
    if guild_id is None:
        return
    gid = str(guild_id)
    if gid in _ai_autoqueue_state:
        if _ai_autoqueue_state[gid].get("enabled"):
            _ai_autoqueue_state[gid]["enabled"] = False
            logger.info("[AI AutoQueue] Disabled auto-queue toggle for guild %s (voice left or disconnected). Config vibe preserved.", gid)


def reset_all_autoqueue() -> None:
    """Ensure auto-queue toggle is disabled across all guilds on bot startup, keeping config vibe in memory/DB."""
    for gid in list(_ai_autoqueue_state.keys()):
        _ai_autoqueue_state[gid]["enabled"] = False
    logger.info("[AI AutoQueue] All autoqueue toggles disabled on startup (all vibes preserved in SQLite).")

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
        # Persist session to SQLite (survives backend restarts for 30 days)
        db.save_session(session_id, user_data, guilds_data, access_token)
        
        separator = "&" if "?" in FRONTEND_URL else "?"
        res = RedirectResponse(url=f"{FRONTEND_URL}{separator}token={session_id}")
        return res

from fastapi.security import OAuth2PasswordBearer
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token", auto_error=False)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    session = db.get_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="Session expired or not found")
    return session

@app.post("/api/logout")
async def logout(token: str = Depends(oauth2_scheme)):
    if token:
        db.delete_session(token)
    return {"status": "success"}

# ---------------------------------------------------------------------------
# Gemini Key Management & Community Contribution Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/ai/keys")
async def list_gemini_keys(user: dict = Depends(get_current_user)):
    """List all contributed Gemini keys with masked strings and live health/cooldown statuses."""
    keys = db.get_all_gemini_keys_for_display()
    is_admin = False
    author = get_simulated_context(None, user).author if bot_instance else None
    if author:
        is_admin = bot_module.is_admin_member(author)

    return {
        "keys": keys,
        "total_active": sum(1 for k in keys if k["is_active"] and k["status"] == "healthy"),
        "total_keys": len(keys),
        "is_admin": is_admin,
    }

@app.post("/api/ai/keys/contribute")
async def contribute_gemini_key(req: ContributeKeyRequest, user: dict = Depends(get_current_user)):
    """Add a user-contributed Gemini API key to the rotation pool.

    Validates key health with Google Gemini:
    - Active/Healthy keys (HTTP 200) join rotation immediately.
    - Exhausted keys (HTTP 429) are saved and placed on cooldown until quota resets.
    - Expired/Rejected keys (HTTP 400/403) are kept in the pool as inactive.
    """
    key = req.api_key.strip().strip('"').strip("'")
    if not key:
        raise HTTPException(status_code=400, detail="API key is required.")

    # 1. Probe key directly against Google Gemini
    probe = await ai_service.verify_gemini_api_key(key)
    probe_status = probe.get("status")
    probe_msg = probe.get("message", "")
    retry_delay = probe.get("retry_delay", 0.0)

    if probe_status == "error":
        # Network connection to Google timed out or failed during probe; save key as active with warning
        logger.warning(f"[AI Key] Probe could not reach Google ({probe_msg}). Saving as active without live verification.")
        probe_status = "healthy"
        probe_msg = "Added without live probe (Google connection timed out during verification)."

    user_info = user.get("user", {})
    user_id = str(user_info.get("id", "anonymous"))
    username = str(user_info.get("username", "Community Contributor"))
    avatar = user_info.get("avatar")

    if probe_status == "healthy":
        res = db.add_gemini_key(
            key, user_id=user_id, username=username, avatar=avatar,
            initial_status="healthy", status_message="", cooldown_seconds=0.0, is_active=True
        )
        msg = "Key verified and active in the pool!"
    elif probe_status == "quota_exceeded":
        res = db.add_gemini_key(
            key, user_id=user_id, username=username, avatar=avatar,
            initial_status="quota_exceeded", status_message=probe_msg, cooldown_seconds=retry_delay, is_active=True
        )
        minutes = max(1, int(retry_delay // 60))
        msg = f"Key added! It is currently exhausted (cooling down for ~{minutes}m) and will automatically activate once quota resets."
    else:  # invalid / expired
        res = db.add_gemini_key(
            key, user_id=user_id, username=username, avatar=avatar,
            initial_status="invalid", status_message=probe_msg, cooldown_seconds=0.0, is_active=False
        )
        msg = f"Key saved to pool as inactive/expired ({probe_msg}). You can keep it here and reactivate it later."

    return {
        "status": "success",
        "key_status": probe_status,
        "message": msg,
        "key_info": res,
    }

@app.delete("/api/ai/keys/{key_id}")
async def remove_gemini_key(key_id: int, user: dict = Depends(get_current_user)):
    """Delete a key from the pool (Admin only)."""
    author = get_simulated_context(None, user).author if bot_instance else None
    if not author or not bot_module.is_admin_member(author):
        raise HTTPException(status_code=403, detail="Only administrators can delete keys.")

    deleted = db.delete_gemini_key(key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found.")
    return {"status": "success"}


@app.get("/api/me")
async def get_me(user: dict = Depends(get_current_user)):
    return {"user": user["user"]}


@app.get("/api/logs", response_class=PlainTextResponse)
async def get_recent_logs(lines: int = 200, user: dict = Depends(get_current_user)):
    """Return the last N lines of the current log file for debugging."""
    log_file = getattr(settings, 'LOG_FILE', 'logs/musicbot-prod.log')
    if not os.path.exists(log_file):
        for candidate in ["logs/musicbot-prod.log", "logs/start.log", "logs/start-linux.log", "logs/bot_runner.log"]:
            if os.path.exists(candidate):
                log_file = candidate
                break
    if not os.path.exists(log_file):
        return "No log file found."
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            tail_lines = all_lines[-max(1, min(lines, 2000)):]
            return "".join(tail_lines)
    except Exception as e:
        return f"Error reading log file {log_file}: {e}"


@app.get("/api/logs/download")
async def download_current_log(file_name: str = "app", user: dict = Depends(get_current_user)):
    """Download log files (app, runner, or start) as an attachment."""
    log_dir = "logs"
    candidates = {
        "app": getattr(settings, 'LOG_FILE', os.path.join(log_dir, "musicbot-prod.log")),
        "runner": os.path.join(log_dir, "bot_runner.log"),
        "start": os.path.join(log_dir, "start-linux.log") if os.path.exists(os.path.join(log_dir, "start-linux.log")) else os.path.join(log_dir, "start.log")
    }
    target = candidates.get(file_name, candidates["app"])
    if not os.path.exists(target):
        raise HTTPException(status_code=404, detail=f"Log file '{file_name}' not found.")
    return FileResponse(target, media_type="text/plain", filename=os.path.basename(target))

@app.get("/api/voice_status")
async def get_voice_status(user: dict = Depends(get_current_user)):
    user_id = int(user["user"]["id"])
    active_sessions = []
    
    logger.debug(f"Checking voice status for user {user_id}")
    for guild in bot_instance.guilds:
        member = guild.get_member(user_id)
        if member:
            logger.debug(f"Found member in guild {guild.name}. Voice state: {member.voice}")
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
                        logger.debug(f"Found user {user_id} in voice channel {vc.name} manually!")
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
    member = guild.get_member(user_id) if guild else None
    if not member:
        # Fallback author using ConsoleAuthor
        member = bot_module.create_console_author()
        member.id = user_id
        member.name = user_data["user"]["username"]
        member.display_name = user_data["user"]["username"]

        avatar_hash = user_data.get("user", {}).get("avatar")
        if avatar_hash:
            avatar_url = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=64"
            class SimulatedAvatar:
                def __init__(self, u):
                    self.url = u
                def __str__(self):
                    return self.url
            member.display_avatar = SimulatedAvatar(avatar_url)
            member.avatar = SimulatedAvatar(avatar_url)

    # Use a generic channel or text channel from state
    state = bot_module.get_music_state(guild) if guild else None
    text_channel = guild.get_channel(state.get("text_channel_id")) if (guild and state) else None
    
    ctx = bot_module.MusicContext(bot=bot_instance, guild=guild, channel=text_channel, author=member)
    return ctx

async def enforce_api_access(ctx, command_name: str):
    mode = bot_module.get_command_mode(command_name)
    if mode == 'admin_only' and not bot_module.is_admin_member(ctx.author):
        raise HTTPException(status_code=403, detail="Only administrators can use this command.")
    if (
        command_name == 'ai_autoqueue'
        and mode == 'vote_if_non_admin'
        and not bot_module.is_admin_member(ctx.author)
    ):
        raise HTTPException(
            status_code=403,
            detail="Only administrators can configure AI Auto-Queue via API.",
        )


def format_song_for_api(song, guild):
    if not song:
        return None
    song_copy = dict(song)
    if song_copy.get('requester_handle') == 'AI Auto-Queue':
        song_copy['requester_avatar'] = '/logo_icon.svg'
        return song_copy
    if not song_copy.get('requester_avatar') and song_copy.get('requester_id'):
        requester_id = song_copy['requester_id']
        # 1. Check guild member cache
        if guild:
            try:
                member = guild.get_member(requester_id)
                if member:
                    if hasattr(member, 'display_avatar') and member.display_avatar:
                        song_copy['requester_avatar'] = str(member.display_avatar.url)
                    elif hasattr(member, 'avatar') and member.avatar:
                        song_copy['requester_avatar'] = str(member.avatar.url)
            except Exception:
                pass
        # 2. Check bot global user cache
        if not song_copy.get('requester_avatar') and bot_instance:
            try:
                user_obj = bot_instance.get_user(int(requester_id))
                if user_obj and hasattr(user_obj, 'display_avatar') and user_obj.display_avatar:
                    song_copy['requester_avatar'] = str(user_obj.display_avatar.url)
            except Exception:
                pass
        # 3. Check active OAuth sessions
        if not song_copy.get('requester_avatar'):
            try:
                avatar_url = db.get_user_avatar(str(requester_id))
                if avatar_url:
                    song_copy['requester_avatar'] = avatar_url
            except Exception:
                pass
    return song_copy

@app.get("/api/queue/{guild_id}")
async def get_queue(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    
    state = bot_module.get_music_state(guild)
    if not state:
        return {"queue": [], "current": None, "volume": 100, "loop": False, "is_playing": False}
        
    queue = [format_song_for_api(s, guild) for s in state.get("queue", [])]
    current = format_song_for_api(state.get("current_song"), guild)
    
    # We need to return serializable objects. `current` might have datetime objects or other non-serializable fields,
    # but based on save_state_to_disk it's mostly a dict of strings/ints.
    
    vc = guild.voice_client if hasattr(guild, 'voice_client') else None
    bot_connected = vc is not None
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

    skip_v = bot_module.get_skip_votes_info(guild, 'skip') if hasattr(bot_module, 'get_skip_votes_info') else (0, 0, 0)
    pause_v = bot_module.get_skip_votes_info(guild, 'pause') if hasattr(bot_module, 'get_skip_votes_info') else (0, 0, 0)
    resume_v = bot_module.get_skip_votes_info(guild, 'resume') if hasattr(bot_module, 'get_skip_votes_info') else (0, 0, 0)
    stop_v = bot_module.get_skip_votes_info(guild, 'stop') if hasattr(bot_module, 'get_skip_votes_info') else (0, 0, 0)

    return {
        "queue": queue,
        "current": current,
        "queue_index": state.get("queue_index", -1),
        "volume": state.get("volume", 100),
        "loop": state.get("loop_enabled", False),
        "is_playing": state.get("is_playing", False),
        "is_paused": is_paused,
        "bot_connected": bot_connected,
        "position": position,
        "skip_votes": skip_v[0],
        "skip_votes_required": skip_v[1],
        "pause_votes": pause_v[0],
        "pause_votes_required": pause_v[1],
        "resume_votes": resume_v[0],
        "resume_votes_required": resume_v[1],
        "stop_votes": stop_v[0],
        "stop_votes_required": stop_v[1]
    }

@app.get("/api/lyrics/{guild_id}")
async def get_lyrics(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    state = bot_module.get_music_state(guild)
    if not state:
        return {"lyrics": None, "error": "No active music session"}
    
    current_song = state.get("current_song")
    if not current_song:
        return {"lyrics": None, "error": "Nothing is currently playing"}
    
    lyrics_data = await asyncio.to_thread(
        lyrics_service.get_lyrics_for_song,
        current_song.get("title", ""),
        current_song.get("artist", ""),
        current_song.get("duration", 0),
        current_song.get("data", "")
    )
    return {"lyrics": lyrics_data}

@app.get("/api/stream/{guild_id}")
async def stream_queue(guild_id: str, request: Request):
    token = request.query_params.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.get_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired or not found")
    guild = await verify_guild_access(guild_id, user)
    
    async def event_generator():
        while True:
            if is_shutting_down or (_api_server and _api_server.should_exit) or await request.is_disconnected():
                break
            try:
                state = bot_module.get_music_state(guild)
                if not state:
                    data = {"queue": [], "current": None, "volume": 100, "loop": False, "is_playing": False, "bot_connected": False}
                else:
                    queue = [format_song_for_api(s, guild) for s in state.get("queue", [])]
                    current = format_song_for_api(state.get("current_song"), guild)
                    
                    vc = guild.voice_client if hasattr(guild, 'voice_client') else None
                    bot_connected = vc is not None
                    is_paused = vc.is_paused() if vc else False
                    
                    position = 0
                    if vc and vc.source:
                        try:
                            original = vc.source.original if hasattr(vc.source, 'original') else vc.source
                            if hasattr(original, 'frames_read'):
                                position = original.frames_read * 0.02
                        except Exception:
                            pass

                    skip_v = bot_module.get_skip_votes_info(guild, 'skip') if hasattr(bot_module, 'get_skip_votes_info') else (0, 0, 0)
                    pause_v = bot_module.get_skip_votes_info(guild, 'pause') if hasattr(bot_module, 'get_skip_votes_info') else (0, 0, 0)
                    resume_v = bot_module.get_skip_votes_info(guild, 'resume') if hasattr(bot_module, 'get_skip_votes_info') else (0, 0, 0)
                    stop_v = bot_module.get_skip_votes_info(guild, 'stop') if hasattr(bot_module, 'get_skip_votes_info') else (0, 0, 0)

                    ai_state = _ai_autoqueue_state.get(guild_id, {})
                    ai_auto_enabled = bool(ai_state.get("enabled", False))
                    if not bot_connected and ai_auto_enabled:
                        disable_autoqueue(guild_id)
                        ai_auto_enabled = False
                        ai_state = _ai_autoqueue_state.get(guild_id, {})

                    data = {
                        "queue": queue,
                        "current": current,
                        "queue_index": state.get("queue_index", -1),
                        "volume": state.get("volume", 100),
                        "loop": state.get("loop_enabled", False),
                        "is_playing": state.get("is_playing", False),
                        "is_paused": is_paused,
                        "bot_connected": bot_connected,
                        "ai_auto_enabled": ai_auto_enabled,
                        "ai_prompt": ai_state.get("prompt", ""),
                        "ai_set_by_name": ai_state.get("set_by_name"),
                        "ai_set_by_avatar": ai_state.get("set_by_avatar"),
                        "position": position,
                        "skip_votes": skip_v[0],
                        "skip_votes_required": skip_v[1],
                        "pause_votes": pause_v[0],
                        "pause_votes_required": pause_v[1],
                        "resume_votes": resume_v[0],
                        "resume_votes_required": resume_v[1],
                        "stop_votes": stop_v[0],
                        "stop_votes_required": stop_v[1]
                    }
                yield f"data: {json.dumps(data)}\n\n"
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SSE error: {e}")
                await asyncio.sleep(5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/queue/{guild_id}")
async def add_song(guild_id: str, req: AddSongRequest, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    await enforce_api_access(ctx, 'yt')
    
    yt_command = bot_instance.get_command('yt')
    if not yt_command:
        raise HTTPException(status_code=500, detail="yt command not found in bot")
        
    # Execute the yt command callback
    # We need to run it safely
    try:
        asyncio.create_task(yt_command.callback(ctx, query=req.url))
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error adding song: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# AI Auto-Queue endpoints
# ---------------------------------------------------------------------------

@app.post("/api/ai/{guild_id}/suggest")
async def ai_suggest(
    guild_id: str,
    req: AISearchRequest,
    user: dict = Depends(get_current_user),
):
    """Ask Gemini to suggest songs based on a prompt. Returns a preview list — nothing is added yet."""
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    await enforce_api_access(ctx, 'ai_autoqueue')

    # Collect titles already in the queue to avoid suggestions duplicating existing tracks
    state = bot_module.get_music_state(guild) or {}
    recent: set[str] = {
        (s.get("title") or "").lower().strip()
        for s in state.get("queue", [])
    }
    if state.get("current_song"):
        recent.add((state["current_song"].get("title") or "").lower().strip())

    try:
        # Ask Gemini for 2× more queries than songs needed so we have fallback candidates
        query_count = max(req.count * 2, 8)
        queries = await ai_service.generate_search_queries(req.prompt, count=query_count)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error("[AI] Gemini error: %s", exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"AI service error: {exc}")

    songs = await ai_service.search_songs(
        queries,
        max_per_query=2,
        blacklist_patterns=list(settings.YT_BLACKLIST_PATTERNS or []),
        recent_titles=recent,
        prompt=req.prompt,
    )
    return {"songs": songs[: max(1, req.count)], "prompt": req.prompt}


@app.post("/api/ai/{guild_id}/confirm")
async def ai_confirm(
    guild_id: str,
    req: AddSongRequest,
    user: dict = Depends(get_current_user),
):
    """Add a single AI-suggested song to the queue with metadata."""
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    await enforce_api_access(ctx, 'yt')

    if not await bot_module.ensure_voice_connected(ctx):
        raise HTTPException(status_code=400, detail="Bot is not connected to a voice channel.")

    if req.title:
        video_data = {
            'title': req.title,
            'webpage_url': req.url,
            'artist': req.artist,
            'thumbnail': req.thumbnail,
            'duration': req.duration or 0,
        }
        song_obj = await bot_module.enqueue_single_song_from_url(
            ctx,
            title=req.title,
            webpage_url=req.url,
            video_data=video_data,
        )
        if not song_obj:
            raise HTTPException(status_code=400, detail="Could not enqueue song (may be blacklisted)")
        return {"status": "success", "title": req.title}

    return await add_song(guild_id, req, user)


@app.post("/api/ai/{guild_id}/confirm_batch")
async def ai_confirm_batch(
    guild_id: str,
    req: AIBatchConfirmRequest,
    user: dict = Depends(get_current_user),
):
    """Add a list of AI-suggested songs to the queue in order without dropping tracks."""
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    await enforce_api_access(ctx, 'yt')

    if not await bot_module.ensure_voice_connected(ctx):
        raise HTTPException(status_code=400, detail="Bot is not connected to a voice channel.")

    added = 0
    for s in req.songs:
        if not s.url:
            continue
        try:
            video_data = {
                'title': s.title or "YouTube Track",
                'webpage_url': s.url,
                'artist': s.artist,
                'thumbnail': s.thumbnail,
                'duration': s.duration or 0,
            }
            song_obj = await bot_module.enqueue_single_song_from_url(
                ctx,
                title=s.title or "YouTube Track",
                webpage_url=s.url,
                video_data=video_data,
            )
            if song_obj:
                added += 1
        except Exception as exc:
            logger.warning("[AI] Failed to add %r in batch: %s", s.url, exc)

    logger.info("[AI] Batch added %d/%d songs for guild %s", added, len(req.songs), guild_id)
    return {"status": "success", "added_count": added}


@app.post("/api/ai/{guild_id}/autoqueue")
async def set_autoqueue(
    guild_id: str,
    cfg: AIAutoQueueConfig,
    user: dict = Depends(get_current_user),
):
    """Enable or disable AI Auto-Queue daemon for a guild and set its parameters."""
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    await enforce_api_access(ctx, 'ai_autoqueue')

    stored = cfg.model_dump()
    u_data = user.get("user") if (user and isinstance(user.get("user"), dict)) else user
    if u_data:
        uid = u_data.get("id")
        member = guild.get_member(int(uid)) if (guild and uid) else None
        uname = (
            (member.display_name if member else None)
            or u_data.get("global_name")
            or u_data.get("username")
            or "User"
        )
        stored["set_by_name"] = uname
        uav = u_data.get("avatar")
        if member and hasattr(member, "display_avatar") and member.display_avatar:
            stored["set_by_avatar"] = str(member.display_avatar.url)
        elif uid and uav:
            stored["set_by_avatar"] = f"https://cdn.discordapp.com/avatars/{uid}/{uav}.png?size=64"

    _ai_autoqueue_state[guild_id] = stored
    try:
        db.save_autoqueue_config(guild_id, stored)
    except Exception as exc:
        logger.warning("[AI AutoQueue] Failed to persist config to DB for guild %s: %s", guild_id, exc)

    logger.info(
        "[AI AutoQueue] Guild %s → enabled=%s, prompt=%r, count=%d, threshold=%d, set_by=%s (saved to SQLite)",
        guild_id, cfg.enabled, cfg.prompt, cfg.count, cfg.threshold, stored.get("set_by_name"),
    )

    if cfg.enabled and cfg.prompt:
        asyncio.create_task(check_and_trigger_autoqueue(guild))

    return {"status": "ok", "config": _ai_autoqueue_state[guild_id]}


@app.get("/api/ai/{guild_id}/autoqueue")
async def get_autoqueue(
    guild_id: str,
    user: dict = Depends(get_current_user),
):
    """Return current AI Auto-Queue configuration for a guild."""
    await verify_guild_access(guild_id, user)
    if guild_id not in _ai_autoqueue_state:
        try:
            persisted = db.load_autoqueue_config(guild_id)
            if persisted:
                _ai_autoqueue_state[guild_id] = persisted
        except Exception:
            pass

    return _ai_autoqueue_state.get(
        guild_id,
        {"enabled": False, "prompt": "", "count": 5, "threshold": 2, "set_by_name": None, "set_by_avatar": None},
    )


# ---------------------------------------------------------------------------
# AI Auto-Queue background daemon and trigger logic
# ---------------------------------------------------------------------------

_autoqueue_filling: set[str] = set()

async def check_and_trigger_autoqueue(guild) -> bool:
    """Check remaining queue length for guild and auto-fill if below threshold.

    Songs left is calculated as: len(queue) - 1 - queue_index
    (e.g., 5 songs total with song at index 2 currently playing = 2 songs left).
    """
    if not guild or not bot_instance:
        return False

    # Do not auto-fill or consume Gemini quota if the bot is not in a voice channel
    voice_client = getattr(guild, 'voice_client', None)
    if not voice_client or not voice_client.is_connected():
        disable_autoqueue(getattr(guild, 'id', None))
        return False

    guild_id = str(guild.id)
    if guild_id in _autoqueue_filling:
        return False

    cfg = _ai_autoqueue_state.get(guild_id)
    if not cfg or not cfg.get("enabled") or not cfg.get("prompt"):
        return False

    state = bot_module.get_music_state(guild) or {}
    queue = state.get("queue", [])
    current_idx = state.get("queue_index", -1)

    if current_idx >= 0 and current_idx < len(queue):
        songs_left = max(0, len(queue) - 1 - current_idx)
    else:
        songs_left = len(queue)

    threshold = int(cfg.get("threshold", 2))
    count = int(cfg.get("count", 5))
    prompt = cfg["prompt"]

    if songs_left < threshold:
        logger.info(
            "[AI AutoQueue] Guild %s: songs_left=%d < threshold=%d — auto-filling %d songs with prompt %r",
            guild_id, songs_left, threshold, count, prompt,
        )
        _autoqueue_filling.add(guild_id)
        try:
            await _daemon_fill_queue(guild, prompt, count, state)
            return True
        except Exception as exc:
            logger.error("[AI AutoQueue] Fill failed for guild %s: %s", guild_id, exc, exc_info=True)
            return False
        finally:
            _autoqueue_filling.discard(guild_id)

    return False


async def _daemon_fill_queue(guild, prompt: str, count: int, state: dict) -> None:
    """Generate songs with Gemini and add them directly to the guild queue."""
    recent: set[str] = {
        (s.get("title") or "").lower().strip()
        for s in state.get("queue", [])
    }
    if state.get("current_song"):
        recent.add((state["current_song"].get("title") or "").lower().strip())

    try:
        queries = await ai_service.generate_search_queries(prompt, count=max(count * 2, 8))
        songs = await ai_service.search_songs(
            queries,
            max_per_query=2,
            blacklist_patterns=list(settings.YT_BLACKLIST_PATTERNS or []),
            recent_titles=recent,
            prompt=prompt,
        )
    except Exception as exc:
        logger.error("[AI AutoQueue] Failed to fetch suggestions: %s", exc)
        return

    added = 0
    # Build a headless (console) context the same way api.py does for non-user calls
    console_author = bot_module.create_console_author()
    console_author.display_name = "AI Auto-Queue"
    console_author.name = "AI Auto-Queue"
    console_author.id = "ai-autoqueue"
    console_author.mention = "AI Auto-Queue"
    text_channel = guild.get_channel(state.get("text_channel_id")) if state else None
    ctx = bot_module.MusicContext(bot=bot_instance, guild=guild, channel=text_channel, author=console_author)

    for song in songs[:count]:
        url = song.get("url")
        if not url:
            continue
        try:
            video_data = {
                'title': song.get('title') or "YouTube Track",
                'webpage_url': url,
                'artist': song.get('artist'),
                'thumbnail': song.get('thumbnail'),
                'duration': song.get('duration', 0),
            }
            song_obj = await bot_module.enqueue_single_song_from_url(
                ctx,
                title=song.get('title') or "YouTube Track",
                webpage_url=url,
                video_data=video_data,
            )
            if song_obj:
                added += 1
        except Exception as exc:
            logger.warning("[AI AutoQueue] Could not add %r: %s", url, exc)

    logger.info("[AI AutoQueue] Queued %d/%d AI songs for guild %s", added, count, guild.id)


@app.post("/api/controls/{guild_id}/skip")
async def skip_song(guild_id: str, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    await enforce_api_access(ctx, 'skip')
    
    cmd = bot_instance.get_command('skip')
    try:
        await cmd.callback(ctx)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/controls/{guild_id}/skipto")
async def skipto_song(guild_id: str, req: IndexRequest, user: dict = Depends(get_current_user)):
    guild = await verify_guild_access(guild_id, user)
    ctx = get_simulated_context(guild, user)
    await enforce_api_access(ctx, 'skipto')
    
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
    await enforce_api_access(ctx, 'remove')
    
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
    await enforce_api_access(ctx, 'clear')
    
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
    await enforce_api_access(ctx, 'join')
    
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
    await enforce_api_access(ctx, 'stop')
    
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
    await enforce_api_access(ctx, 'pause')
    
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
    await enforce_api_access(ctx, 'resume')
    
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
    await enforce_api_access(ctx, 'loop')
    
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
    await enforce_api_access(ctx, 'volume')
    
    cmd = bot_instance.get_command('volume')
    try:
        await cmd.callback(ctx, volume=req.volume)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

_api_server = None
_api_task = None

def start_api_server(host="0.0.0.0", port=8000):
    global _api_server, _api_task, is_shutting_down
    if _api_task and not _api_task.done():
        logger.info("API server is already running.")
        return
    is_shutting_down = False
    import uvicorn
    # Pass log_config=None so Uvicorn uses the bot's existing UTF-8 logging setup
    # instead of closing sys.stdout and overwriting existing loggers.
    # Set timeout_graceful_shutdown=1 so Uvicorn will not hang indefinitely on open SSE connections.
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        timeout_graceful_shutdown=1,
    )
    _api_server = uvicorn.Server(config)
    _api_task = asyncio.create_task(_api_server.serve())

def stop_api_server():
    global _api_server, is_shutting_down
    is_shutting_down = True
    if _api_server:
        _api_server.should_exit = True

async def wait_api_server_shutdown(timeout=2.0):
    global _api_task, _api_server, is_shutting_down
    is_shutting_down = True
    if _api_server:
        _api_server.should_exit = True
    if _api_task and not _api_task.done():
        try:
            await asyncio.wait_for(_api_task, timeout=timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            _api_task.cancel()
            try:
                await asyncio.wait_for(_api_task, timeout=1.0)
            except Exception:
                pass
