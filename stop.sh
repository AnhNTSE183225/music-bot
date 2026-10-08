#!/usr/bin/env bash
# MusicBot Stopper - Stops MusicBot and associated services cleanly

echo -e "\e[36mStopping MusicBot...\e[0m\n"

# 1. Stop Linux background process via bot.pid if present
if [ -f bot.pid ]; then
    BOT_PID=$(cat bot.pid)
    if kill -0 $BOT_PID 2>/dev/null; then
        echo -e "\e[33mStopping MusicBot PID $BOT_PID...\e[0m"
        kill $BOT_PID
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
        echo -e "\e[32mMusicBot background process stopped.\e[0m"
    fi
    rm -f bot.pid
fi

# 2. Stop Tailscale Funnel if present
if [ -f tailscale_funnel.pid ]; then
    sudo -v 2>/dev/null
    TS_PID=$(cat tailscale_funnel.pid)
    if sudo kill -0 $TS_PID 2>/dev/null; then
        echo -e "\e[33mStopping Tailscale Funnel PID $TS_PID...\e[0m"
        sudo kill $TS_PID
        WAIT_COUNT=0
        TIMEOUT=8
        while sudo kill -0 $TS_PID 2>/dev/null; do
            sleep 1
            WAIT_COUNT=$((WAIT_COUNT + 1))
            if [ $WAIT_COUNT -ge $TIMEOUT ]; then
                echo -e "\e[31mTailscale Funnel did not exit within ${TIMEOUT}s. Force killing...\e[0m"
                sudo kill -9 $TS_PID 2>/dev/null
                break
            fi
        done
        echo -e "\e[32mTailscale Funnel stopped.\e[0m"
    fi
    rm -f tailscale_funnel.pid
fi

# 3. Stop Windows processes if running on Windows / Git Bash
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
    PIDS=$(powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -like '*bot.py*' } | Select-Object -ExpandProperty ProcessId" 2>/dev/null)
    for p in $PIDS; do
        p=$(echo "$p" | tr -d '\r\n')
        if [ -n "$p" ]; then
            echo -e "\e[33mTerminating MusicBot Windows process (PID $p)...\e[0m"
            taskkill.exe //F //PID "$p" 2>/dev/null
        fi
    done
fi

echo -e "\n\e[32mAll MusicBot services stopped successfully.\e[0m"
