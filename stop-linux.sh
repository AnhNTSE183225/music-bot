#!/usr/bin/env bash
# MusicBot Linux Stopper - Stops background processes cleanly

echo -e "\e[36mStopping MusicBot Linux Background Services\e[0m\n"

# Stop Bot
if [ -f bot.pid ]; then
    BOT_PID=$(cat bot.pid)
    if kill -0 $BOT_PID 2>/dev/null; then
        echo -e "\e[33mStopping MusicBot (PID $BOT_PID)...\e[0m"
        kill $BOT_PID
        
        # Wait for the process to exit cleanly
        while kill -0 $BOT_PID 2>/dev/null; do
            sleep 1
        done
        echo -e "\e[32mMusicBot stopped successfully.\e[0m"
    else
        echo -e "\e[33mMusicBot PID file exists but process is not running.\e[0m"
    fi
    rm -f bot.pid
else
    echo -e "\e[33mNo bot.pid found. MusicBot is not running.\e[0m"
fi

# Stop Tailscale Funnel
if [ -f tailscale_funnel.pid ]; then
    TS_PID=$(cat tailscale_funnel.pid)
    if kill -0 $TS_PID 2>/dev/null; then
        echo -e "\e[33mStopping Tailscale Funnel (PID $TS_PID)...\e[0m"
        kill $TS_PID
        
        # Wait for the process to exit cleanly
        while kill -0 $TS_PID 2>/dev/null; do
            sleep 1
        done
        echo -e "\e[32mTailscale Funnel stopped successfully.\e[0m"
    else
        echo -e "\e[33mTailscale Funnel PID file exists but process is not running.\e[0m"
    fi
    rm -f tailscale_funnel.pid
else
    echo -e "\e[33mNo tailscale_funnel.pid found. Tailscale Funnel is not running.\e[0m"
fi

echo -e "\n\e[32mAll services stopped successfully!\e[0m"
