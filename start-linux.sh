#!/usr/bin/env bash
# MusicBot Linux Runner - Handles system deps, venv setup, and background execution

# Ensure logs directory exists
LOGS_DIR="./logs"
SAVE_DIR="./logs-saved"
if [ ! -d "$LOGS_DIR" ]; then
    mkdir -p "$LOGS_DIR"
fi

# Mount tmpfs (RAM disk) for ./logs on Linux if not already mounted to eliminate USB flash wear
if ! mountpoint -q "$LOGS_DIR" 2>/dev/null; then
    if sudo -n mount -t tmpfs -o size=64M,noatime,nosuid tmpfs "$LOGS_DIR" 2>/dev/null; then
        echo -e "\e[32mOK: Mounted RAM disk (tmpfs) on $LOGS_DIR to eliminate USB flash wear.\e[0m"
    fi
fi

# Persist RAM logs to disk on exit or crash
save_crash_logs() {
    mkdir -p "$SAVE_DIR"
    if [ -d "$LOGS_DIR" ]; then
        cp -f "$LOGS_DIR"/*.log "$SAVE_DIR/" 2>/dev/null || true
    fi
}
trap save_crash_logs EXIT INT TERM

START_LOG_FILE="$LOGS_DIR/start-linux.log"

# Tee all script output to start-linux.log while maintaining terminal output
exec > >(tee -a "$START_LOG_FILE") 2>&1

echo -e "\e[36mMusicBot Linux Background Runner\e[0m\n"
echo "Logging start-linux execution to $START_LOG_FILE"

# Configure which config file to use (e.g., config.prod.yaml)
export MUSICBOT_CONFIG_FILE="config.prod.yaml"

# 1. Install missing system dependencies
echo -e "\e[33mChecking system dependencies (may ask for sudo password)...\e[0m"
DEPS_TO_INSTALL=""
for pkg in ffmpeg python3-venv python3-dev build-essential; do
    if ! dpkg -s $pkg >/dev/null 2>&1; then
        DEPS_TO_INSTALL="$DEPS_TO_INSTALL $pkg"
    fi
done

if [ -n "$DEPS_TO_INSTALL" ]; then
    echo "Installing missing dependencies: $DEPS_TO_INSTALL"
    sudo apt update
    sudo apt install -y $DEPS_TO_INSTALL
    if [ $? -ne 0 ]; then
        echo -e "\e[31mERROR: Failed to install system dependencies.\e[0m"
        exit 1
    fi
else
    echo -e "\e[32mOK: All system dependencies are installed.\e[0m"
fi

# Select a Python launcher
if command -v python3 &>/dev/null; then
    PYTHON_EXE="python3"
elif command -v python &>/dev/null; then
    PYTHON_EXE="python"
else
    echo -e "\e[31mERROR: Python was not found.\e[0m"
    exit 1
fi

# Create venv if it doesn't exist
if [ ! -d "./venv" ]; then
    echo -e "\e[33mVirtual environment not found. Creating...\e[0m"
    $PYTHON_EXE -m venv venv
    if [ $? -ne 0 ]; then
        echo -e "\e[31mERROR: Failed to create virtual environment.\e[0m"
        exit 1
    fi
    echo -e "\e[32mOK: Virtual environment created\e[0m"
fi

VENV_PYTHON="./venv/bin/python"

# Install/update Python dependencies
VENV_MARKER="./venv/.installed"
DEPS_HEALTHY=true

$VENV_PYTHON -c "import yaml, httpx, dotenv, fastapi, uvicorn" >/dev/null 2>&1
if [ $? -ne 0 ]; then
    DEPS_HEALTHY=false
fi

REQUIREMENTS_CHANGED=false
if [ -f "$VENV_MARKER" ]; then
    if [ "requirements.txt" -nt "$VENV_MARKER" ]; then
        REQUIREMENTS_CHANGED=true
    fi
fi

if [ ! -f "$VENV_MARKER" ] || [ "$REQUIREMENTS_CHANGED" = true ] || [ "$DEPS_HEALTHY" = false ]; then
    echo -e "\e[33mInstalling dependencies from requirements.txt...\e[0m"
    $VENV_PYTHON -m pip install --upgrade pip
    $VENV_PYTHON -m pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo -e "\e[31mERROR: Failed to install Python dependencies.\e[0m"
        exit 1
    fi
    touch "$VENV_MARKER"
    echo -e "\e[32mOK: Dependencies installed\e[0m"
fi

# Portable Node.js for yt-dlp JS challenges
if ! command -v node > /dev/null; then
    NODE_DIR="./venv/node-v20.11.1-linux-x64"
    if [ ! -d "$NODE_DIR" ]; then
        echo -e "\e[33mDownloading portable Node.js for yt-dlp JS challenges...\e[0m"
        curl -sL "https://nodejs.org/dist/v20.11.1/node-v20.11.1-linux-x64.tar.xz" | tar -xJ -C ./venv
        echo -e "\e[32mOK: Node.js downloaded\e[0m"
    fi
    export PATH="$PWD/$NODE_DIR/bin:$PATH"
fi

echo -e "\e[36mChecking for pip and yt-dlp updates...\e[0m"
$VENV_PYTHON -m pip install -U pip yt-dlp >/dev/null 2>&1

echo -e "\e[36mConfiguring Environment...\e[0m"

# Load and validate .env file
if [ -f ".env" ]; then
    set -a
    source .env 2>/dev/null || true
    set +a
else
    echo -e "\e[33m[!] WARNING: .env file not found!\e[0m"
    if [ -f ".env.example" ]; then
        echo -e "\e[33m    Creating initial .env from .env.example...\e[0m"
        cp .env.example .env
        echo -e "\e[31m    ACTION REQUIRED: Edit .env to set your DISCORD_TOKEN and GEMINI_API_KEY.\e[0m"
    fi
fi

# Verify GEMINI_API_KEY for AI features
if [ -z "$GEMINI_API_KEY" ] || [[ "$GEMINI_API_KEY" == *"your_"* ]]; then
    echo -e "\e[33m[!] NOTICE: GEMINI_API_KEY is not set in .env\e[0m"
    echo -e "\e[33m    AI features will say 'AI unavailable' until a key is added.\e[0m"
    echo -e "\e[33m    Get a free Gemini key at: https://aistudio.google.com\e[0m\n"
else
    echo -e "\e[32mOK: GEMINI_API_KEY configured.\e[0m\n"
fi

RUNTIME_MODE="prod"
MODE_CANDIDATE=$($VENV_PYTHON -c "import yaml, os; c=yaml.safe_load(open(os.getenv('MUSICBOT_CONFIG_FILE', 'config.yaml'),'r',encoding='utf-8')) or {}; print(str((c.get('runtime',{}) or {}).get('mode','prod')).strip().lower())" 2>/dev/null)
if [ $? -eq 0 ] && [ -n "$MODE_CANDIDATE" ]; then
    if [ "$MODE_CANDIDATE" = "debug" ] || [ "$MODE_CANDIDATE" = "prod" ]; then
        RUNTIME_MODE="$MODE_CANDIDATE"
    fi
fi

TIMESTAMP=$(date +"%Y%m%d-%H%M%S")
PROD_LOG_FILE="$LOGS_DIR/musicbot-prod.log"

if [ "$RUNTIME_MODE" = "debug" ]; then
    DEBUG_LOG_FILE="$LOGS_DIR/musicbot-debug-$TIMESTAMP.log"
    export MUSICBOT_RUNTIME_MODE="debug"
    export MUSICBOT_LOG_LEVEL="DEBUG"
    export MUSICBOT_PLAYBACK_DEBUG_METRICS="true"
    export MUSICBOT_LOG_FILE="$DEBUG_LOG_FILE"
    echo -e "\e[33mRuntime mode: DEBUG\e[0m"
    echo -e "\e[33mApp log file: $DEBUG_LOG_FILE\e[0m"
else
    export MUSICBOT_RUNTIME_MODE="prod"
    export MUSICBOT_LOG_LEVEL="INFO"
    export MUSICBOT_PLAYBACK_DEBUG_METRICS="false"
    export MUSICBOT_LOG_FILE="$PROD_LOG_FILE"
    echo -e "\e[32mRuntime mode: PROD\e[0m"
    echo -e "\e[32mApp log file: $PROD_LOG_FILE\e[0m"
fi

# Starting Bot in Background (stops any existing instance first)
CURRENT_PID=$$
if [ -f bot.pid ]; then
    OLD_PID=$(cat bot.pid 2>/dev/null || true)
    if [ -n "$OLD_PID" ] && kill -0 $OLD_PID 2>/dev/null; then
        echo -e "\e[33mStopping existing MusicBot process (PID $OLD_PID)...\e[0m"
        kill $OLD_PID 2>/dev/null || true
        WAIT_COUNT=0
        while kill -0 $OLD_PID 2>/dev/null && [ $WAIT_COUNT -lt 4 ]; do
            sleep 1
            WAIT_COUNT=$((WAIT_COUNT + 1))
        done
        if kill -0 $OLD_PID 2>/dev/null; then
            kill -9 $OLD_PID 2>/dev/null || true
            sleep 1
        fi
        echo -e "\e[32mOK: Previous process stopped.\e[0m"
    fi
    rm -f bot.pid
fi

# Clean up any lingering orphan bot.py processes
REMAINING_BOT_PIDS=$(ps -ef | grep '[b]ot\.py' | awk '{print $2}' || true)
if [ -n "$REMAINING_BOT_PIDS" ]; then
    for pid in $REMAINING_BOT_PIDS; do
        if [ "$pid" != "$CURRENT_PID" ]; then
            kill -9 $pid 2>/dev/null || true
        fi
    done
fi

# Ensure port 8000 is released before launching
if command -v fuser >/dev/null 2>&1; then
    fuser -k 8000/tcp >/dev/null 2>&1 || true
fi

echo -e "\e[36mStarting bot in the background...\e[0m"
export PYTHONUNBUFFERED=1
# We pipe /dev/null to stdin to cleanly disable console bridge
nohup $VENV_PYTHON -u ./bot.py < /dev/null > "$LOGS_DIR/bot_runner.log" 2>&1 &
BOT_PID=$!
echo $BOT_PID > bot.pid
sleep 2
if ! kill -0 $BOT_PID 2>/dev/null; then
    echo -e "\e[31mERROR: Bot failed to start! Last lines of bot_runner.log:\e[0m"
    tail -n 25 "$LOGS_DIR/bot_runner.log"
    exit 1
fi
echo -e "\e[32mBot started (PID $BOT_PID)! Runner log: $LOGS_DIR/bot_runner.log, App log: $MUSICBOT_LOG_FILE\e[0m"

# Starting Tailscale Funnel in Background
TS_RUNNING=false
if [ -f tailscale_funnel.pid ]; then
    TS_PID=$(cat tailscale_funnel.pid 2>/dev/null || true)
    if [ -n "$TS_PID" ] && kill -0 $TS_PID 2>/dev/null; then
        TS_RUNNING=true
    fi
fi

if [ "$TS_RUNNING" = false ]; then
    ACTIVE_TS_PID=$(pgrep -f "tailscale funnel.*8000" | head -n 1 || true)
    if [ -n "$ACTIVE_TS_PID" ]; then
        echo $ACTIVE_TS_PID > tailscale_funnel.pid
        TS_RUNNING=true
    fi
fi

if [ "$TS_RUNNING" = true ]; then
    echo -e "\e[33mTailscale Funnel is already running (PID $(cat tailscale_funnel.pid 2>/dev/null)).\e[0m"
else
    echo -e "\e[36mStarting Tailscale Funnel on port 8000...\e[0m"
    # Refresh sudo credentials if available without blocking CI
    sudo -n -v 2>/dev/null || true
    (sudo -n tailscale funnel 8000 2>/dev/null || tailscale funnel 8000) > "$LOGS_DIR/tailscale_funnel.log" 2>&1 &
    TS_SUB_PID=$!
    sleep 1
    ACTUAL_TS_PID=$(pgrep -f "tailscale funnel.*8000" | head -n 1 || true)
    echo "${ACTUAL_TS_PID:-$TS_SUB_PID}" > tailscale_funnel.pid
    echo -e "\e[32mTailscale funnel started! Logs are in $LOGS_DIR/tailscale_funnel.log\e[0m"
fi

echo -e "\n\e[32mAll services started successfully in the background!\e[0m"
