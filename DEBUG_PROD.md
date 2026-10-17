# Production Debugging & Status Guide (MusicBot)

This guide documents how to check runtime status, inspect logs, diagnose issues, and debug MusicBot in production (e.g., running on a Raspberry Pi off a USB drive or Linux VPS).

---

## 1. Quick Status & Health Checks

Run these commands from the `music-bot/` directory:

### Check Process Liveness
```bash
# Check if the bot process is active
if [ -f bot.pid ] && kill -0 $(cat bot.pid) 2>/dev/null; then
    echo "MusicBot is RUNNING (PID: $(cat bot.pid))"
else
    echo "MusicBot is STOPPED"
fi

# Check Tailscale Funnel
if [ -f tailscale_funnel.pid ] && kill -0 $(cat tailscale_funnel.pid) 2>/dev/null; then
    echo "Tailscale Funnel is RUNNING (PID: $(cat tailscale_funnel.pid))"
else
    echo "Tailscale Funnel is STOPPED"
fi

# Check listening ports (FastAPI on 8000)
ss -tulpn | grep 8000
```

### Check System & Memory
```bash
# Check RAM disk (tmpfs) mount on logs (prevents USB wear)
df -h ./logs

# Check Pi CPU temperature and load
vcgencmd measure_temp
uptime
```

---

## 2. Inspecting Production Logs

All active logs live in **RAM (`tmpfs`)** at `./logs/` to eliminate flash drive wear.

### Log File Breakdown
| Log File | Contents |
| :--- | :--- |
| `logs/musicbot-prod.log` | **Main application log**: discord.py gateway, queue transitions, API server, SQLite transactions, AI auto-queue. Auto-rotates at 10MB (2 backups). |
| `logs/bot_runner.log` | **Raw process output**: Python stdout/stderr, early startup tracebacks, unhandled fatal exceptions before logger initialized. |
| `logs/start-linux.log` | **Runner setup log**: Apt dependencies, virtualenv checks, pip/yt-dlp updates, .env validation. |
| `logs/tailscale_funnel.log` | **Tunnel proxy log**: Tailscale port 8000 forwarding status. |
| `logs-saved/` | **Persistent crash dumps**: On shutdown or crash, logs are automatically copied from RAM to this directory on the USB drive. |

### Useful Log Inspection Commands
```bash
# Live stream the application log
tail -f logs/musicbot-prod.log

# View the last 100 log lines
tail -n 100 logs/musicbot-prod.log

# Filter for errors and warnings only
grep -E "ERROR|WARNING|CRITICAL|Traceback" logs/musicbot-prod.log | tail -n 50

# Check for unhandled startup or Python crashes
tail -n 50 logs/bot_runner.log

# View preserved crash logs from previous boot
ls -la logs-saved/
tail -n 100 logs-saved/musicbot-prod.log
```

---

## 3. Remote Debugging via Web API (No SSH Required)

When accessing via the web interface or browser, use these endpoints:

### 1. View Live Logs (Plain Text)
```http
GET http://<pi-ip-or-funnel-url>:8000/api/logs?lines=200
```
*Returns the last `lines` (default: 200, max: 2000) of `musicbot-prod.log`.*

### 2. Download Log Files
```http
GET http://<pi-ip-or-funnel-url>:8000/api/logs/download?file_name=app
GET http://<pi-ip-or-funnel-url>:8000/api/logs/download?file_name=runner
GET http://<pi-ip-or-funnel-url>:8000/api/logs/download?file_name=start
```
*Downloads the requested log file directly as a text file attachment.*

### 3. Check Voice & Channel Status
```http
GET http://<pi-ip-or-funnel-url>:8000/api/voice_status
```

### 4. Check Gemini Key Pool Status
```http
GET http://<pi-ip-or-funnel-url>:8000/api/ai/keys
```
*Inspects active keys, quota cooldown timestamps, and call counts.*

---

## 4. SQLite Database Inspection (`musicbot.db`)

The database uses WAL mode with memory temp tables (`PRAGMA temp_store=MEMORY`).

```bash
# Check persistent queue state per guild
sqlite3 musicbot.db "SELECT guild_id, is_playing, queue_index, json_array_length(queue) AS queue_len, datetime(saved_at, 'unixepoch') FROM guild_music_state;"

# Check Gemini keys health and cooldowns
sqlite3 musicbot.db "SELECT id, added_by_username, is_active, status, total_uses, datetime(cooldown_until, 'unixepoch') AS cooldown_until FROM gemini_keys;"

# Check active web sessions
sqlite3 musicbot.db "SELECT session_id, username, datetime(created_at, 'unixepoch'), datetime(expires_at, 'unixepoch') FROM sessions ORDER BY created_at DESC LIMIT 5;"

# Database integrity check
sqlite3 musicbot.db "PRAGMA integrity_check;"
```

---

## 5. Troubleshooting Common Production Incidents

### Issue A: Bot connected to Discord, but no audio plays
1. Run `tail -n 50 logs/musicbot-prod.log | grep -i "ffmpeg"`.
2. Verify `ffmpeg` is installed: `which ffmpeg`.
3. Check yt-dlp challenges / update:
   ```bash
   ./venv/bin/python -m pip install -U yt-dlp
   ```
4. Verify portable Node.js exists for yt-dlp JavaScript challenges:
   ```bash
   node -v || ./venv/node-v20.11.1-linux-x64/bin/node -v
   ```

### Issue B: "PrivilegedIntentsRequired" or loop on Gateway
- Go to [Discord Developer Portal](https://discord.com/developers/applications/) -> select your application -> **Bot** tab.
- Ensure **Message Content Intent** and **Server Members Intent** are toggled **ON**.

### Issue C: OAuth "Invalid redirect_uri"
- Verify that `http://<domain-or-ip>:8000/callback` is added to **Redirects** in the Discord Developer Portal under OAuth2.
- Check `.env` to verify whether `DISCORD_CLIENT_ID` or `DEV_DISCORD_CLIENT_ID` is active.

### Issue D: AI suggestions say "AI unavailable"
- Inspect key pool status:
  ```bash
  sqlite3 musicbot.db "SELECT status, cooldown_until FROM gemini_keys;"
  ```
- If keys are marked `quota_exceeded`, wait for cooldown to expire or contribute a fresh free Gemini key in the web UI.

---

## 6. Clean Service Restart

To cleanly stop and restart all production services:
```bash
./start-linux.sh
```
*The script automatically stops lingering bot and port 8000 processes gracefully within 3 seconds, flushes RAM logs to `logs-saved/`, checks dependencies, and relaunches both MusicBot and Tailscale Funnel in the background.*
