# Inspector Gadget — Log & Traffic Inspector

A local web tool for PeerPlay QA and Dev — streams Android logcat and live network traffic from a connected device running Merge Cruise, with built-in Claude AI analysis.

**Download the latest release:** [imperial-qa-hub Releases](https://github.com/PeerPlayGames/imperial-qa-hub/releases)

---

## Features

### Logcat Panel
- Real-time ADB logcat stream filtered to `com.peerplay.megamerge`
- Category filter presets: Unity, Ads, Network, Analytics, Sentry, Errors Only
- Per-level toggles: V / D / I / W / E
- Text search with inline highlighting
- Auto-scroll with manual override detection
- Multi-line Unity log grouping
- Export all logs as `.txt`

### Network Traffic Panel
- Embedded mitmproxy (no Charles needed) — intercepts all HTTPS traffic from the device
- Full request + response detail: headers, bodies, JSON syntax highlighting
- Base64 / MessagePack auto-decode for game state payloads (`/state/update`, checkpoints)
- Filter presets: Game API, ADS SDKs, Analytics, Errors 4xx/5xx
- **Mixpanel analytics view** — decoded event cards showing event name, user ID, chapter, credits, key properties
- **Copy BQ Query button** — one click generates a ready-to-run BigQuery SQL for any analytics event
- Diagnostic logging for all intercepted traffic (visible in terminal)

### Claude AI Analysis
- Click any logcat row or network request — Claude explains what it means and what action is needed
- Auto-triggers on Error logs and 4xx/5xx responses
- Requires an Anthropic API key (see setup below)

### General
- Time-descending sort synced between both panels — toggle from either Time column header
- Device status badge with auto-reconnect
- PID tracking — auto-restarts when the app relaunches

---

## Prerequisites

| Dependency | Install | Verify |
|---|---|---|
| **Python 3.9+** | Pre-installed on macOS | `python3 --version` |
| **ADB** | `brew install android-platform-tools` | `adb version` |
| **mitmproxy** | Installed via `pip3` (see below) | `mitmdump --version` |

---

## Installation

### Option A: macOS .app (pre-built)

1. Download `InspectorGadget-vX.X.X-macOS.zip` from [Releases](https://github.com/PeerPlayGames/imperial-qa-hub/releases)
2. Unzip and move to `/Applications`
3. **First launch only:** right-click the app, then click Open (macOS blocks unsigned apps)
4. The tool opens `http://localhost:5001` in your browser automatically

### Option B: From source (Dev)

```bash
git clone https://github.com/PeerPlayGames/mc-logcat.git
cd mc-logcat
pip3 install -r requirements.txt
./mc-logcat.sh
# Opens http://localhost:5001
```

**requirements.txt installs:** `flask`, `flask-socketio`, `mitmproxy`, `msgpack`

---

## Device Setup

### Step 1: Enable USB Debugging (one-time)

1. On the Android device: **Settings > About Phone > tap Build number 7 times** (enables Developer Options)
2. **Settings > Developer Options > USB Debugging > ON**
3. Connect device via USB
4. Accept the **"Allow USB debugging?"** prompt on the device

The tool shows a green **Connected** badge once the device is detected.

### Step 2: Start the Proxy

1. In Inspector Gadget, go to the **Network Traffic** panel
2. Enter port `8082` (default) and click **Start Proxy**
3. The tool displays your Mac's local IP (e.g., `192.168.1.100:8082`)

### Step 3: Configure Device Wi-Fi Proxy

On the Android device:

1. **Settings > Wi-Fi** > long-press your connected network > **Modify Network**
2. Expand **Advanced options**
3. Set **Proxy** to **Manual**
4. **Proxy hostname:** your Mac's IP (shown in the tool, e.g., `192.168.1.100`)
5. **Proxy port:** `8082`
6. Tap **Save**

> **Alternative (ADB):** If you can't modify Wi-Fi settings on the device, use ADB:
> ```bash
> adb shell settings put global http_proxy <mac-ip>:8082
> ```
> To clear later:
> ```bash
> adb shell settings put global http_proxy :0
> ```

### Step 4: Install the SSL Certificate (one-time per device)

The mitmproxy CA certificate must be installed on the device so the proxy can inspect HTTPS traffic.

**Method A — Via the tool (recommended):**

1. In Inspector Gadget, click **Download SSL Cert** — this pushes the cert to the device via ADB and opens it in the browser
2. On the device, tap the downloaded `mitmproxy-ca-cert.pem` file

**Method B — Manual download:**

1. On the device browser, navigate to `http://mitm.it` (only works while proxy is active)
2. Download the Android certificate (`.pem` file)

**Then install the certificate:**

1. **Settings > Security > Encryption & credentials > Install a certificate > CA certificate**
2. Confirm the warning prompt
3. Select the downloaded `.pem` file
4. Name it anything (e.g., `mitmproxy`)

> **Important — Android 11+:** The certificate MUST be installed as a **CA certificate**, not under "VPN & app user certificate." On Android 14+, the path may be: **Settings > Security & privacy > More security settings > Encryption & credentials > Install a certificate > CA certificate**.

> **Important — Android 16:** Samsung devices may have a slightly different path: **Settings > Biometrics and security > Other security settings > Install from device storage** — then select **CA certificate**.

### Step 5: Verify

1. Open Merge Cruise on the device
2. Network requests should appear in the **Network Traffic** panel within seconds
3. Check the terminal running Inspector Gadget for `[proxy] response:` log lines — these confirm traffic is flowing through the proxy

If no traffic appears, see the Troubleshooting section below.

---

## Claude AI Setup (optional)

Create a `.env` file in the project root (same folder as `server.py`):

```
ANTHROPIC_API_KEY=sk-ant-...
```

Get a key at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys).
Add credits at [console.anthropic.com/settings/billing](https://console.anthropic.com/settings/billing).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| **"No Device" badge** | Check USB cable, accept debugging prompt, run `adb devices` in terminal |
| **App detected but no logs** | Make sure Merge Cruise is running on the device |
| **Proxy not intercepting** | Verify device Wi-Fi proxy matches the IP:port shown in the tool. Check terminal for `[proxy] response:` lines |
| **No `[proxy] response:` lines in terminal** | Traffic isn't reaching the proxy. Check that Mac and device are on the same Wi-Fi network, and proxy IP/port are correct |
| **`[proxy] ERROR capturing ...` in terminal** | Traffic reaches the proxy but parsing fails. Check the error message for details |
| **SSL errors on device / HTTPS not decrypted** | Reinstall the mitmproxy cert under **CA Certificates** (not VPN/App). See Step 4 above |
| **Some HTTPS traffic missing (e.g., analytics)** | Some SDKs may bypass the system proxy. Check terminal for diagnostic `[proxy] response:` lines to confirm |
| **Certificate install option not visible** | On Android 14+: Settings > Security & privacy > More security settings > Encryption & credentials > Install a certificate > CA certificate |
| **Claude shows auth error** | Check `.env` file has a valid `ANTHROPIC_API_KEY` |
| **Claude shows credit error** | Add credits at console.anthropic.com/settings/billing |
| **`.app` blocked by macOS** | Right-click the app, then click Open (first launch only) |

---

## Architecture

- **Backend:** Python Flask + Flask-SocketIO (`server.py`)
- **Frontend:** Single HTML file with embedded CSS + JS (`templates/index.html`)
- **Logcat:** `adb logcat --pid=<PID> -v threadtime`
- **Proxy:** mitmproxy `DumpMaster` in background asyncio thread, with diagnostic logging
- **AI:** Anthropic `claude-haiku-4-5` via REST API
- **Bundling:** PyInstaller > macOS `.app`

## Project structure

```
mc-logcat/
├── server.py           # Flask + SocketIO backend, ADB, proxy, Claude API
├── device_proxy.py     # UIAutomator automation for device Wi-Fi proxy set/clear
├── launcher.py         # macOS .app entry point
├── templates/
│   └── index.html      # Frontend (HTML + CSS + JS, single file)
├── mc-logcat.spec      # PyInstaller build config
├── build.sh            # Release builder
├── mc-logcat.sh        # Dev launcher
├── requirements.txt    # Python dependencies
└── .env                # Anthropic API key (not committed)
```
