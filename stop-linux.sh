#!/usr/bin/env bash
# MusicBot Linux Stopper - Stops background processes cleanly

echo -e "\e[36mStopping MusicBot Linux Background Services\e[0m\n"

# Stop Bot
if [ -f bot.pid ]; then
    BOT_PID=$(cat bot.pid)
    if kill -0 $BOT_PID 2>/dev/null; then
        echo -e "\e[33mStopping MusicBot (PID $BOT_PID)...\e[0m"
        kill $BOT_PID
        
        # Wait for the process to exit cleanly with timeout fallback
        WAIT_COUNT=0
        TIMEOUT=8
        while kill -0 $BOT_PID 2>/dev/null; do
            sleep 1
            WAIT_COUNT=$((WAIT_COUNT + 1))
            if [ $WAIT_COUNT -ge $TIMEOUT ]; then
                echo -e "\e[31mMusicBot did not exit cleanly within ${TIMEOUT}s. Force killing (SIGKILL)...\e[0m"
                kill -9 $BOT_PID 2>/dev/null
                break
            fi
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
    # Cache sudo credentials in foreground for clean prompt
    sudo -v
    TS_PID=$(cat tailscale_funnel.pid)
    if sudo kill -0 $TS_PID 2>/dev/null; then
        echo -e "\e[33mStopping Tailscale Funnel (PID $TS_PID)...\e[0m"
        sudo kill $TS_PID
        
        # Wait for the process to exit cleanly with timeout fallback
        WAIT_COUNT=0
        TIMEOUT=8
        while sudo kill -0 $TS_PID 2>/dev/null; do
            sleep 1
            WAIT_COUNT=$((WAIT_COUNT + 1))
            if [ $WAIT_COUNT -ge $TIMEOUT ]; then
                echo -e "\e[31mTailscale Funnel did not exit cleanly within ${TIMEOUT}s. Force killing (SIGKILL)...\e[0m"
                sudo kill -9 $TS_PID 2>/dev/null
                break
            fi
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
