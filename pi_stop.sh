#!/usr/bin/env bash

# Ensure we are in the directory of the script
cd "$(dirname "$0")"

echo "Stopping MusicBot Docker containers..."
docker compose down

if [ -f tailscale_funnel.pid ]; then
    PID=$(cat tailscale_funnel.pid)
    if kill -0 $PID 2>/dev/null; then
        echo "Stopping Tailscale Funnel (PID $PID)..."
        kill $PID
    else
        echo "Tailscale Funnel is not running, but PID file existed."
    fi
    rm tailscale_funnel.pid
else
    echo "Tailscale Funnel PID file not found. It might not be running or was started manually."
fi

echo "All stopped."
