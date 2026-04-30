#!/bin/bash
# PawOo Gold Signal Bot v4.0 - Startup Script
echo "Starting PawOo Gold Signal Bot v4.0..."

# Start performance monitor in background
python3 performance_monitor.py &
MONITOR_PID=$!
echo "Performance monitor started (PID: $MONITOR_PID)"

# Start main bot in background
python3 bot.py &
BOT_PID=$!
echo "Signal bot started (PID: $BOT_PID)"

# Wait for either to exit
wait -n $BOT_PID $MONITOR_PID

# If one dies, kill the other and exit (Railway will auto-restart)
echo "A process exited. Shutting down..."
kill $BOT_PID $MONITOR_PID 2>/dev/null
exit 1
