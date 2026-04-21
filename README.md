# Merge Cruise Logcat Viewer

Real-time Android logcat viewer for **Merge Cruise** — PeerPlay DevTools.

Streams, filters, and explains logs from a connected Android device — live in your browser.

---

## For QA & Team Members — Quick Start

### Option A: Download the app (no technical setup needed)

1. Go to [**Releases**](../../releases) → download `MergeCruiseLogcat-vX.X.X-macOS.zip`
2. Unzip → drag `MergeCruiseLogcat.app` to your `/Applications` folder
3. **First time only:** right-click the app → Open → click "Open" in the security dialog
4. Connect your Android device via USB
5. Done — the app opens your browser automatically

> **ADB required:** Make sure [Android Platform Tools](https://developer.android.com/tools/releases/platform-tools) is installed and `adb` is on your PATH.

---

### Option B: Run from source (developers)

```bash
git clone https://github.com/PeerPlayGames/mc-logcat.git
cd mc-logcat
pip3 install -r requirements.txt
./mc-logcat.sh
```

---

## Features

| Feature | Detail |
|---|---|
| Auto device detection | Detects connect/disconnect every 2s |
| Auto PID tracking | Re-attaches logcat if app restarts |
| Multi-select filters | Combine UNITY + ADS + ERRORS freely |
| Level toggles | V / D / I / W / E independently |
| Live search | Filter by tag or message with highlight |
| Claude AI analysis | Click any row for instant error explanation |
| Export | Download all logs as `.txt` |
| Auto-scroll | Smart — pauses on scroll up, resumes at bottom |

### Filter categories

| Filter | What it captures |
|---|---|
| UNITY | Game engine, IL2CPP, native process |
| ADS | AppLovin MAX, LevelPlay, UnityAds, FAN |
| NETWORK | Server calls, Realm DB, billing |
| ANALYTICS | Mixpanel, TAPP, Firebase |
| SENTRY | Error/crash tracking |
| FACEBOOK | Facebook SDK |
| ERRORS ONLY | E + W levels across all categories |

---

## Building a new release

```bash
./build.sh 1.2.0
```

Then attach `release/MergeCruiseLogcat-v1.2.0-macOS.zip` to a new GitHub Release.

---

## Project structure

```
mc-logcat/
├── server.py          # Flask + SocketIO backend, ADB integration, Claude API
├── launcher.py        # macOS .app entry point
├── templates/
│   └── index.html     # Frontend (HTML + CSS + JS, single file)
├── mc-logcat.spec     # PyInstaller build config
├── build.sh           # Release builder
├── mc-logcat.sh       # Dev launcher (source mode)
└── requirements.txt
```

---

## Requirements

- macOS 12+
- Android device with USB debugging enabled
- `adb` installed (`brew install android-platform-tools`)
- `ANTHROPIC_API_KEY` env var set (for Claude AI explanations)
