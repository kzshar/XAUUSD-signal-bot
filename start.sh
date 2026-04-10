#!/bin/bash
# Start both Signal Bot and Performance Monitor
echo "Starting XAU/USD Signal Bot v3.0..."
python3 performance_monitor.py &
MONITOR_PID=$!
echo "Performance Monitor started (PID: $MONITOR_PID)"

python3 bot.py &
BOT_PID=$!
echo "Signal Bot started (PID: $BOT_PID)"

# Wait for either to exit
wait -n $BOT_PID $MONITOR_PID

# If one dies, kill the other and exit
echo "A process exited. Shutting down..."
kill $BOT_PID $MONITOR_PID 2>/dev/null
exit 1
