#!/usr/bin/env bash

# Ensure we are in the directory of the script
cd "$(dirname "$0")"

echo "Starting MusicBot via Docker Compose..."
docker compose up -d

echo "Checking Tailscale Funnel..."
if [ -f tailscale_funnel.pid ] && kill -0 $(cat tailscale_funnel.pid) 2>/dev/null; then
    echo "Tailscale Funnel is already running."
else
    echo "Starting Tailscale Funnel on port 8000 in the background..."
    # Note: If your funnel command is 5000, change this to 5000 (though the bot uses 8000 natively)
    tailscale funnel 8000 > tailscale_funnel.log 2>&1 &
    echo $! > tailscale_funnel.pid
    echo "Tailscale funnel started! Logs are in tailscale_funnel.log"
fi

echo "Done! MusicBot and Funnel are up."
