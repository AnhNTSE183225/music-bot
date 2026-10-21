#!/usr/bin/env bash
# MusicBot Linux Stopper - Stops background processes cleanly with guaranteed process cleanup

echo -e "\e[36mStopping MusicBot Linux Background Services\e[0m\n"

# 1. Stop Bot via PID file if present
if [ -f bot.pid ]; then
    BOT_PID=$(cat bot.pid)
    if kill -0 $BOT_PID 2>/dev/null; then
        echo -e "\e[33mStopping MusicBot (PID $BOT_PID) (SIGTERM)...\e[0m"
        kill $BOT_PID 2>/dev/null || true
        
        WAIT_COUNT=0
        TIMEOUT=5
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

# 2. Fallback: find and kill any remaining bot.py processes (using ps aux | grep bot.py)
REMAINING_BOT_PIDS=$(ps aux | grep -E '[b]ot\.py' | awk '{print $2}')
if [ -n "$REMAINING_BOT_PIDS" ]; then
    echo -e "\e[33mFound lingering MusicBot processes (PIDs: $REMAINING_BOT_PIDS). Stopping...\e[0m"
    for pid in $REMAINING_BOT_PIDS; do
        kill $pid 2>/dev/null || true
    done
    sleep 1
    for pid in $REMAINING_BOT_PIDS; do
        if kill -0 $pid 2>/dev/null; then
            echo -e "\e[31mForce killing (SIGKILL) lingering process $pid...\e[0m"
            kill -9 $pid 2>/dev/null || true
        fi
    done
fi

# 3. Free port 8000 if anything is still listening on it
if command -v fuser &>/dev/null; then
    fuser -k -9 8000/tcp 2>/dev/null || true
elif command -v lsof &>/dev/null; then
    PORT_PIDS=$(lsof -t -i:8000 2>/dev/null || true)
    if [ -n "$PORT_PIDS" ]; then
        kill -9 $PORT_PIDS 2>/dev/null || true
    fi
fi

# 4. Stop Tailscale Funnel
sudo -n -v 2>/dev/null || true
if [ -f tailscale_funnel.pid ]; then
    TS_PID=$(cat tailscale_funnel.pid)
    if sudo kill -0 $TS_PID 2>/dev/null; then
        echo -e "\e[33mStopping Tailscale Funnel (PID $TS_PID)...\e[0m"
        sudo kill $TS_PID 2>/dev/null || true
        
        WAIT_COUNT=0
        TIMEOUT=5
        while sudo kill -0 $TS_PID 2>/dev/null; do
            sleep 1
            WAIT_COUNT=$((WAIT_COUNT + 1))
            if [ $WAIT_COUNT -ge $TIMEOUT ]; then
                echo -e "\e[31mTailscale Funnel did not exit within ${TIMEOUT}s. Force killing (SIGKILL)...\e[0m"
                sudo kill -9 $TS_PID 2>/dev/null || true
                break
            fi
        done
        echo -e "\e[32mTailscale Funnel stopped successfully.\e[0m"
    fi
    rm -f tailscale_funnel.pid
fi

# Fallback: check for any lingering tailscale funnel processes
REMAINING_TS=$(ps aux | grep -E '[t]ailscale funnel' | awk '{print $2}')
if [ -n "$REMAINING_TS" ]; then
    for pid in $REMAINING_TS; do
        sudo kill -9 $pid 2>/dev/null || true
    done
fi

echo -e "\n\e[32mAll services stopped successfully!\e[0m"
