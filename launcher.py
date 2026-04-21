#!/usr/bin/env python3
"""
Merge Cruise Logcat — macOS app entry point.
Opens the browser automatically and runs the server.
"""
import os
import sys
import threading
import time
import subprocess
import webbrowser

# When packaged by PyInstaller, data files live next to the executable
if getattr(sys, 'frozen', False):
    base_dir = sys._MEIPASS
    # Set the working dir so Flask finds templates/
    os.chdir(base_dir)
else:
    base_dir = os.path.dirname(os.path.abspath(__file__))

# Make sure server.py can import from the right place
sys.path.insert(0, base_dir)

PORT = 5001

def open_browser():
    """Wait for Flask to be ready then open the browser."""
    for _ in range(30):
        time.sleep(0.3)
        try:
            import urllib.request
            urllib.request.urlopen(f'http://localhost:{PORT}', timeout=1)
            webbrowser.open(f'http://localhost:{PORT}')
            return
        except Exception:
            continue

# Open browser in background thread
threading.Thread(target=open_browser, daemon=True).start()

# Start the server (blocks)
from server import app, socketio, device_monitor

monitor_thread = threading.Thread(target=device_monitor, daemon=True)
monitor_thread.start()

print(f'\n  ⚓  Merge Cruise Logcat — PeerPlay DevTools')
print(f'  🌊  http://localhost:{PORT}\n')

socketio.run(app, host='0.0.0.0', port=PORT, debug=False,
             use_reloader=False, allow_unsafe_werkzeug=True)
