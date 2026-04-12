# MusicBot

A Discord music bot built with discord.py and yt-dlp.

## Prerequisites

- Python 3.8 or higher
- Windows PowerShell (or equivalent)

## Installation & Setup

Simply run:
```powershell
.\start.ps1
```

The script automatically handles everything:
- Creates a Python virtual environment (`venv/`) if needed
- Activates the environment
- Installs dependencies from `requirements.txt` (on first run and when requirements change)
- Runs the bot

That's it—no separate setup required! ✨

## Virtual Environment Benefits

- **Isolation**: Project dependencies don't affect your system Python
- **Reproducibility**: Exact versions pinned in `requirements.txt` 
- **Portability**: Works consistently across different machines
- **Cleanliness**: Easy to remove (just delete the `venv/` folder)

## Project Structure

- `bot.py` - Main bot application
- `settings.py` - Configuration settings
- `config.yaml` - Bot configuration
- `requirements.txt` - Python dependencies (pinned versions)
- `.env` - Environment variables (secrets)
- `venv/` - Virtual environment (created during setup, excluded from git)

## Updating Dependencies

To add or upgrade packages:
1. Activate venv: `.\venv\Scripts\Activate.ps1`
2. Install/upgrade: `pip install --upgrade package_name`
3. Freeze to requirements.txt: `pip freeze > requirements.txt`
4. Next time you run `.\start.ps1`, dependencies will auto-update
5. Commit the updated `requirements.txt` to git

## Web API For Frontend

MusicBot exposes an HTTP API for the `music-bot-fe` dashboard.

- Base URL: `http://127.0.0.1:8080/api`
- Endpoints:
	- `GET /state`
	- `POST /volume`
	- `POST /skip-vote`
	- `GET /youtube/search?q=...`
	- `POST /queue/youtube`

Configure in `config.yaml`:

	Admin login uses credentials from `.env`:

	- `MUSICBOT_WEB_ADMIN_USERNAME`
	- `MUSICBOT_WEB_ADMIN_PASSWORD`
	- `MUSICBOT_WEB_ADMIN_TOKEN_TTL_SECONDS` optionally controls session length

```yaml
web_api:
	enabled: true
	host: "127.0.0.1"
	port: 8080
	allowed_origins:
		- "http://localhost:5173"
```

