#!/bin/bash
# Merge Cruise Logcat — one-command launcher
# Usage: ./mc-logcat.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=5001

# Kill any previous instances (including stale ones from previous runs)
lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null
sleep 0.5

echo ""
echo "  ⚓  Merge Cruise Logcat — PeerPlay DevTools"
echo "  ──────────────────────────────────────────"
echo "  Starting server on http://localhost:$PORT ..."
echo ""

# Start server in background
python3 "$SCRIPT_DIR/server.py" &
SERVER_PID=$!

# Wait for Flask to be ready
for i in $(seq 1 20); do
  if curl -s "http://localhost:$PORT" > /dev/null 2>&1; then
    break
  fi
  sleep 0.3
done

# Open browser
open "http://localhost:$PORT"
echo "  ✓  Browser opened at http://localhost:$PORT"
echo "  ✓  Server PID: $SERVER_PID"
echo ""
echo "  Watching: com.peerplay.megamerge"
echo "  Press Ctrl+C to stop"
echo ""

# Forward logs to terminal and clean up on exit
trap "echo ''; echo '  Stopped.'; kill $SERVER_PID 2>/dev/null" EXIT INT TERM
wait $SERVER_PID
