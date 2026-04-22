# Merge Cruise Logcat Viewer

A local web tool for PeerPlay QA and Dev — streams Android logcat and live network traffic from a connected device running Merge Cruise, with built-in Claude AI analysis.

**Download the latest release:** [imperial-qa-hub Releases](https://github.com/PeerPlayGames/imperial-qa-hub/releases)

---

## Features

### Logcat Panel
- Real-time ADB logcat stream filtered to `com.peerplay.megamerge`
- Category filter presets: Unity · Ads · Network · Analytics · Sentry · Errors Only
- Per-level toggles: V / D / I / W / E
- Text search with inline highlighting
- Auto-scroll with manual override detection
- Multi-line Unity log grouping
- Export all logs as `.txt`

### Network Traffic Panel
- Embedded proxy (no Charles needed) — intercepts all HTTPS traffic from the device
- Full request + response detail: headers, bodies, JSON syntax highlighting
- Base64 / MessagePack auto-decode in JSON bodies
- Filter presets: Game API · ADS SDKs · Analytics · Errors 4xx/5xx
- **Mixpanel analytics view** — decoded event cards showing event name, user ID, chapter, credits, key properties
- **Copy BQ Query button** — one click generates a ready-to-run BigQuery SQL for any analytics event

### Claude AI Analysis
- Click any logcat row or network request → Claude explains what it means and what action is needed
- Auto-triggers on Error logs and 4xx/5xx responses
- Requires an Anthropic API key (see setup below)

### General
- Time-descending sort synced between both panels — toggle from either Time column header
- Device status badge with auto-reconnect
- PID tracking — auto-restarts when the app relaunches

---

## Installation (macOS .app)

1. Download `MergeCruiseLogcat-vX.X.X-macOS.zip` from [Releases](https://github.com/PeerPlayGames/imperial-qa-hub/releases)
2. Unzip and move `MergeCruiseLogcat.app` to `/Applications`
3. **First launch only:** right-click → Open (macOS blocks unsigned apps by default)
4. The tool opens `http://localhost:5001` in your browser automatically

---

## Device Setup (required once per device)

### 1. Enable USB Debugging
Settings → Developer Options → USB Debugging → ON

> Don't see Developer Options? Go to Settings → About Phone → tap **Build number** 7 times.

### 2. Connect via USB
Plug in the device. Accept the **"Allow USB debugging?"** prompt on the device.

The tool shows a green **Connected** badge and auto-detects the app PID once Merge Cruise is running.

---

## Proxy Setup (for Network Traffic panel)

### 1. Start the proxy
Network Traffic panel → enter port `8082` → click **Start Proxy**.

The tool displays your Mac's IP address (e.g. `192.168.1.100:8082`).

### 2. Configure the device Wi-Fi proxy
On the Android device:
- Settings → Wi-Fi → long-press your network → Modify Network
- Set **Proxy** to Manual → Hostname: `<Mac IP>` · Port: `8082`
- Save

### 3. Install the SSL certificate
Allows the proxy to inspect HTTPS traffic.

- In the tool, click **Download SSL Cert** — the cert opens on the device browser automatically via ADB
- On the device: tap the downloaded `.pem` → install as **CA Certificate**
  - Settings → Security → Install certificate → CA Certificate
- Name it anything (e.g. `mitmproxy`)

> **Android 16:** install under CA Certificates specifically, not the regular certificate store.

### 4. Verify
Open Merge Cruise. Requests appear in the Network Traffic panel within seconds.

---

## Claude AI Setup (optional)

Create a `.env` file in the same folder as `server.py`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Get a key at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).
Add credits at [console.anthropic.com/settings/billing](https://console.anthropic.com/settings/billing).

---

## Running from source (Dev)

```bash
git clone https://github.com/PeerPlayGames/mc-logcat.git
cd mc-logcat
pip3 install flask flask-socketio mitmproxy anthropic
./mc-logcat.sh
# Opens http://localhost:5001
```

---

## Building a release

```bash
./build.sh 1.0.X
# Output: release/MergeCruiseLogcat-v1.0.X-macOS.zip
```

Create a GitHub release on [imperial-qa-hub](https://github.com/PeerPlayGames/imperial-qa-hub/releases) and attach the zip.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| "No Device" badge | Check USB cable, accept debugging prompt, run `adb devices` in terminal |
| App detected but no logs | Make sure Merge Cruise is running |
| Proxy not intercepting | Check device Wi-Fi proxy matches the IP/port shown in tool |
| SSL errors on device | Reinstall cert under CA Certificates (not VPN/App store) |
| Claude shows auth error | Check `.env` file has a valid `ANTHROPIC_API_KEY` |
| Claude shows credit error | Add credits at console.anthropic.com/settings/billing |
| `.app` blocked by macOS | Right-click → Open (first launch only) |

---

## Architecture

- **Backend:** Python Flask + Flask-SocketIO (`server.py`)
- **Frontend:** Single HTML file with embedded CSS + JS (`templates/index.html`)
- **Logcat:** `adb logcat --pid=<PID> -v threadtime`
- **Proxy:** mitmproxy `DumpMaster` in background asyncio thread
- **AI:** Anthropic `claude-haiku-4-5` via REST API
- **Bundling:** PyInstaller → macOS `.app`

## Project structure

```
mc-logcat/
├── server.py           # Flask + SocketIO backend, ADB, proxy, Claude API
├── launcher.py         # macOS .app entry point
├── templates/
│   └── index.html      # Frontend (HTML + CSS + JS, single file)
├── mc-logcat.spec      # PyInstaller build config
├── build.sh            # Release builder
├── mc-logcat.sh        # Dev launcher
└── requirements.txt
```
