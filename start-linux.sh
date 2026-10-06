#!/usr/bin/env bash
# MusicBot Linux Runner - Handles system deps, venv setup, and background execution

echo -e "\e[36mMusicBot Linux Background Runner\e[0m\n"

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

$VENV_PYTHON -c "import yaml" >/dev/null 2>&1
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
RUNTIME_MODE="prod"
MODE_CANDIDATE=$($VENV_PYTHON -c "import yaml; c=yaml.safe_load(open('config.yaml','r',encoding='utf-8')) or {}; print(str((c.get('runtime',{}) or {}).get('mode','prod')).strip().lower())" 2>/dev/null)
if [ $? -eq 0 ] && [ -n "$MODE_CANDIDATE" ]; then
    if [ "$MODE_CANDIDATE" = "debug" ] || [ "$MODE_CANDIDATE" = "prod" ]; then
        RUNTIME_MODE="$MODE_CANDIDATE"
    fi
fi

LOGS_DIR="./logs"
if [ ! -d "$LOGS_DIR" ]; then
    mkdir -p "$LOGS_DIR"
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
else
    export MUSICBOT_RUNTIME_MODE="prod"
    export MUSICBOT_LOG_LEVEL="INFO"
    export MUSICBOT_PLAYBACK_DEBUG_METRICS="false"
    export MUSICBOT_LOG_FILE="$PROD_LOG_FILE"
    echo -e "\e[32mRuntime mode: PROD\e[0m"
fi

# Starting Bot in Background
if [ -f bot.pid ] && kill -0 $(cat bot.pid) 2>/dev/null; then
    echo -e "\e[33mMusicBot is already running (PID $(cat bot.pid)).\e[0m"
else
    echo -e "\e[36mStarting bot in the background...\e[0m"
    # We pipe /dev/null to stdin to cleanly disable console bridge
    nohup $VENV_PYTHON ./bot.py < /dev/null > bot_runner.log 2>&1 &
    echo $! > bot.pid
    echo -e "\e[32mBot started (PID $(cat bot.pid))! Check bot_runner.log and logs/ for output.\e[0m"
fi

# Starting Tailscale Funnel in Background
if [ -f tailscale_funnel.pid ] && kill -0 $(cat tailscale_funnel.pid) 2>/dev/null; then
    echo -e "\e[33mTailscale Funnel is already running (PID $(cat tailscale_funnel.pid)).\e[0m"
else
    echo -e "\e[36mStarting Tailscale Funnel on port 8000...\e[0m"
    tailscale funnel 8000 > tailscale_funnel.log 2>&1 &
    echo $! > tailscale_funnel.pid
    echo -e "\e[32mTailscale funnel started! Logs are in tailscale_funnel.log\e[0m"
fi

echo -e "\n\e[32mAll services started successfully in the background!\e[0m"
