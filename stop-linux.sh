#!/usr/bin/env bash
# MusicBot Linux Stopper - Stops background processes cleanly with guaranteed process cleanup

echo -e "\e[36mStopping MusicBot Linux Background Services\e[0m\n"

# 1. Stop Bot via PID file if present
if [ -f bot.pid ]; then
    BOT_PID=$(cat bot.pid 2>/dev/null || true)
    if [ -n "$BOT_PID" ] && kill -0 $BOT_PID 2>/dev/null; then
        echo -e "\e[33mStopping MusicBot (PID $BOT_PID) (SIGTERM)...\e[0m"
        kill $BOT_PID 2>/dev/null || true
        
        WAIT_COUNT=0
        TIMEOUT=3
        while kill -0 $BOT_PID 2>/dev/null; do
            sleep 1
            WAIT_COUNT=$((WAIT_COUNT + 1))
            if [ $WAIT_COUNT -ge $TIMEOUT ]; then
                echo -e "\e[31mMusicBot (PID $BOT_PID) did not exit within ${TIMEOUT}s. Force killing (SIGKILL)...\e[0m"
                kill -9 $BOT_PID 2>/dev/null || true
                break
            fi
        done
        echo -e "\e[32mPID file process stopped.\e[0m"
    fi
    rm -f bot.pid
fi

# 2. Fallback: find and kill any remaining bot.py processes (using ps -ef | grep bot.py)
CURRENT_PID=$$
REMAINING_BOT_PIDS=$(ps -ef | grep '[b]ot\.py' | awk '{print $2}' || true)
if [ -n "$REMAINING_BOT_PIDS" ]; then
    echo -e "\e[33mFound lingering MusicBot processes (PIDs: $REMAINING_BOT_PIDS). Stopping...\e[0m"
    for pid in $REMAINING_BOT_PIDS; do
        if [ "$pid" != "$CURRENT_PID" ]; then
            kill -9 $pid 2>/dev/null || true
        fi
    done
fi

# 3. Stop Tailscale Funnel if active
sudo -n -v 2>/dev/null || true
if [ -f tailscale_funnel.pid ]; then
    TS_PID=$(cat tailscale_funnel.pid 2>/dev/null || true)
    if [ -n "$TS_PID" ] && sudo -n kill -0 $TS_PID 2>/dev/null; then
        echo -e "\e[33mStopping Tailscale Funnel (PID $TS_PID)...\e[0m"
        sudo -n kill -9 $TS_PID 2>/dev/null || true
        echo -e "\e[32mTailscale Funnel stopped.\e[0m"
    fi
    rm -f tailscale_funnel.pid
fi

# Fallback: check for any lingering tailscale funnel processes
REMAINING_TS=$(ps -ef | grep '[t]ailscale funnel' | awk '{print $2}' || true)
if [ -n "$REMAINING_TS" ]; then
    for pid in $REMAINING_TS; do
        if [ "$pid" != "$CURRENT_PID" ]; then
            sudo -n kill -9 $pid 2>/dev/null || true
        fi
    done
fi

echo -e "\n\e[32mAll services stopped successfully!\e[0m"
