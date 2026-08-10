#!/usr/bin/env bash
# MusicBot Runner - Handles venv setup and execution

echo -e "\e[36mMusicBot\e[0m\n"

# Select a Python launcher for initial venv creation
if command -v python3 &>/dev/null; then
    PYTHON_EXE="python3"
elif command -v python &>/dev/null; then
    PYTHON_EXE="python"
else
    echo -e "\e[31mERROR: Python was not found. Install Python 3 and try again.\e[0m"
    read -p "Press Enter to exit..."
    exit 1
fi

# Create venv if it doesn't exist
if [ ! -d "./venv" ]; then
    echo -e "\e[33mVirtual environment not found. Creating...\e[0m"
    $PYTHON_EXE -m venv venv
    if [ $? -ne 0 ]; then
        echo -e "\e[31mERROR: Failed to create virtual environment.\e[0m"
        read -p "Press Enter to exit..."
        exit 1
    fi
    echo -e "\e[32mOK: Virtual environment created\e[0m"
fi

VENV_PYTHON="./venv/bin/python"
if [ ! -f "$VENV_PYTHON" ]; then
    # Fallback to Windows layout inside bash (e.g. Git Bash on Windows)
    VENV_PYTHON="./venv/Scripts/python"
    if [ ! -f "$VENV_PYTHON" ]; then
        echo -e "\e[31mERROR: venv Python not found.\e[0m"
        read -p "Press Enter to exit..."
        exit 1
    fi
fi

# Install/update dependencies on first run, on requirements change, or when yaml is missing
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
        echo -e "\e[31mERROR: Failed to install dependencies.\e[0m"
        read -p "Press Enter to exit..."
        exit 1
    fi
    touch "$VENV_MARKER"
    echo -e "\e[32mOK: Dependencies installed\e[0m"
fi

echo ""
echo -e "\e[36mChecking for pip and yt-dlp updates...\e[0m"
$VENV_PYTHON -m pip install -U pip yt-dlp

echo ""
echo -e "\e[36mStarting bot...\e[0m"
echo ""

# Resolve runtime mode from config.yaml (default: prod)
RUNTIME_MODE="prod"
MODE_CANDIDATE=$($VENV_PYTHON -c "import yaml; c=yaml.safe_load(open('config.yaml','r',encoding='utf-8')) or {}; print(str((c.get('runtime',{}) or {}).get('mode','prod')).strip().lower())" 2>/dev/null)
if [ $? -eq 0 ] && [ -n "$MODE_CANDIDATE" ]; then
    if [ "$MODE_CANDIDATE" = "debug" ] || [ "$MODE_CANDIDATE" = "prod" ]; then
        RUNTIME_MODE="$MODE_CANDIDATE"
    fi
fi

# Create external log file targets
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
    echo -e "\e[33mDebug log file: $DEBUG_LOG_FILE\e[0m"
    echo -e "\e[33mLog level override: DEBUG\e[0m"
else
    export MUSICBOT_RUNTIME_MODE="prod"
    export MUSICBOT_LOG_LEVEL="INFO"
    export MUSICBOT_PLAYBACK_DEBUG_METRICS="false"
    export MUSICBOT_LOG_FILE="$PROD_LOG_FILE"

    echo -e "\e[32mRuntime mode: PROD\e[0m"
    echo -e "\e[32mProduction log file: $PROD_LOG_FILE\e[0m"
    echo -e "\e[32mLog level override: INFO\e[0m"
fi

echo ""

# Run the bot
$VENV_PYTHON ./bot.py

read -p "Press Enter to exit..."
