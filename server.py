#!/usr/bin/env python3
"""
Merge Cruise Logcat Viewer — PeerPlay DevTools
"""

import os
import re
import subprocess
import threading
import time
from datetime import datetime
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'mc-logcat-peerplay-2025'
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading',
                    logger=False, engineio_logger=False)

PACKAGE = 'com.peerplay.megamerge'
MAX_BUFFER = 5000

state = {
    'device_serial': None,
    'device_connected': False,
    'pid': None,
    'logcat_proc': None,
    'log_buffer': [],
    'lock': threading.Lock(),
    'logcat_stop': threading.Event(),
}

# ── Tag categorisation ──────────────────────────────────────────────────────

CATEGORY_MAP = {
    'unity':     {'exact': {'Unity', 'UNITY', 'IL2CPP', 'rplay.megamerge', 'ActivityThread'},
                  'prefix': []},
    'ads':       {'exact': {'AppLovinSdk', 'AppLovinQualityService', 'LevelPlaySDK', 'UnityAds',
                             'FBAudienceNetwork', 'Ads', 'Inneractive_info', 'sdk5Events',
                             'AdInternalSettings', 'Kumiho'},
                  'prefix': []},
    'network':   {'exact': {'Realm', 'BillingClient', 'ConnectivityManager'},
                  'prefix': ['cr_', 'cn_', 'chromium']},
    'analytics': {'exact': {'TAPP', 'FA', 'firebase', 'FirebaseApp', 'FirebaseSessions',
                             'FirebaseCrashlytics', 'FirebaseInitProvider', 'IDS_TAG',
                             'EventGDTLogger', 'SessionFirelogPublisher', 'SessionLifecycleClient',
                             'SessionLifecycleService', 'SessionsDependencies', 'HttpFlagsLoader'},
                  'prefix': []},
    'sentry':    {'exact': {'Sentry', 'libcrashlytics'},
                  'prefix': []},
    'facebook':  {'exact': {'com.facebook.unity.FB'},
                  'prefix': ['com.facebook']},
}

# Keywords in Unity tag messages used to sub-categorise
UNITY_MSG_CATS = {
    'analytics': ['[mixpanel]', '[tapp]', 'mixpanel', 'impression_', 'analytics'],
    'ads':       ['[adsorch', 'applovin', 'levelplay', '[ads]', 'adsmanager', 'adunit'],
    'network':   ['request:', 'response:', 'https://', 'http://', 'realm', 'status code'],
    'sentry':    ['sentry:', '[sentry]', 'sentry.'],
    'facebook':  ['facebook', 'fbsdk'],
}

_STACKTRACE_RE = re.compile(
    r'^(UnityEngine\.|System\.|Cysharp\.|Zenject\.|Assets/|Packages/|Best\.|'
    r'mixpanel\.|Ads\.|Common\.|GameMain\.|Scapes\.|[A-Z][a-zA-Z0-9_.]+[.:<][A-Za-z_<>])'
)


def is_stacktrace(msg: str) -> bool:
    if not msg or not msg.strip():
        return True
    return bool(_STACKTRACE_RE.match(msg))


def categorise(tag: str, message: str = '') -> str:
    for cat, rules in CATEGORY_MAP.items():
        if tag in rules['exact']:
            if cat == 'unity' and tag in ('Unity', 'UNITY'):
                msg_l = message.lower()
                for sub_cat, keywords in UNITY_MSG_CATS.items():
                    if any(k in msg_l for k in keywords):
                        return sub_cat
            return cat
        if any(tag.startswith(p) for p in rules['prefix']):
            return cat
    return 'system'


# ── Log line parser ─────────────────────────────────────────────────────────
# threadtime: MM-DD HH:MM:SS.mmm  PID  TID L TAG    : msg

_RE_THREAD = re.compile(
    r'^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(.*?)\s*:\s*(.*)'
)
_RE_TIME = re.compile(
    r'^(\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\s+([VDIWEF])/([^\(]+)\(\s*\d+\):\s*(.*)'
)

# Strip Unity rich-text color tags from messages
_COLOR_TAG_RE = re.compile(r'</?[Cc]olor[^>]*>')


def clean_msg(msg: str) -> str:
    return _COLOR_TAG_RE.sub('', msg).strip()


def parse_line(line: str):
    m = _RE_THREAD.match(line)
    if m:
        ts, pid, tid, level, tag, msg = (
            m.group(1), m.group(2), m.group(3),
            m.group(4), m.group(5).strip(), clean_msg(m.group(6))
        )
        return {'timestamp': ts, 'pid': pid, 'tid': tid,
                'level': level, 'tag': tag, 'message': msg,
                'category': categorise(tag, msg)}

    m2 = _RE_TIME.match(line)
    if m2:
        ts, level, tag, msg = (
            m2.group(1), m2.group(2),
            m2.group(3).strip(), clean_msg(m2.group(4))
        )
        return {'timestamp': ts, 'pid': '', 'tid': '',
                'level': level, 'tag': tag, 'message': msg,
                'category': categorise(tag, msg)}

    return None


# ── ADB helpers ─────────────────────────────────────────────────────────────

def adb(*args, serial=None, timeout=5):
    cmd = ['adb'] + (['-s', serial] if serial else []) + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ''


def get_device():
    out = adb('devices')
    for line in out.splitlines()[1:]:
        if '\tdevice' in line:
            return line.split('\t')[0]
    return None


def get_pid(serial):
    out = adb('shell', 'pidof', PACKAGE, serial=serial)
    parts = out.split()
    return parts[0] if parts else None


# ── Multi-line Unity log grouping ────────────────────────────────────────────
# Unity emits a log as N consecutive lines sharing the same timestamp+TID.
# Line 1  = the actual message.
# Lines 2+ = stack trace OR continuation of message (e.g. "Unauthorized").
# We merge them into a single emitted entry.

def flush_unity_group(group: list, store_and_emit_fn):
    if not group:
        return
    first = group[0]
    extras = []
    for line in group[1:]:
        msg = line['message']
        if msg and not is_stacktrace(msg):
            extras.append(msg)

    merged = dict(first)
    if extras:
        merged['message'] = first['message'] + '\n' + '\n'.join(extras)
        merged['category'] = categorise(first['tag'], merged['message'])
    store_and_emit_fn(merged)


# ── Logcat thread ───────────────────────────────────────────────────────────

def run_logcat(serial: str, pid: str, stop_evt: threading.Event):
    # Use --pid to capture ALL logs for this process (Unity, Ads SDKs, Analytics,
    # Facebook, Sentry — they all run in the same process).
    cmd = ['adb', '-s', serial, 'logcat', f'--pid={pid}', '-v', 'threadtime']

    def store_and_emit(log):
        with state['lock']:
            state['log_buffer'].append(log)
            if len(state['log_buffer']) > MAX_BUFFER:
                state['log_buffer'] = state['log_buffer'][-MAX_BUFFER:]
        socketio.emit('log', log)

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        state['logcat_proc'] = proc

        unity_group = []      # buffered Unity lines for current group
        group_key = None      # (timestamp, tid) of current group

        for raw in proc.stdout:
            if stop_evt.is_set():
                break
            raw = raw.rstrip()
            if not raw:
                continue

            parsed = parse_line(raw)
            if not parsed:
                continue

            tag = parsed['tag']

            if tag in ('Unity', 'UNITY'):
                key = (parsed['timestamp'], parsed['tid'])
                if key == group_key:
                    unity_group.append(parsed)
                else:
                    # New group — flush previous
                    flush_unity_group(unity_group, store_and_emit)
                    unity_group = [parsed]
                    group_key = key
            else:
                # Non-Unity line — flush any pending Unity group first
                flush_unity_group(unity_group, store_and_emit)
                unity_group = []
                group_key = None
                store_and_emit(parsed)

        flush_unity_group(unity_group, store_and_emit)
        proc.terminate()

    except Exception as e:
        socketio.emit('sys_msg', {'text': f'Logcat error: {e}', 'type': 'error'})
    finally:
        state['logcat_proc'] = None


def stop_logcat():
    state['logcat_stop'].set()
    proc = state.get('logcat_proc')
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            pass
    state['logcat_proc'] = None


def restart_logcat(serial, pid):
    stop_logcat()
    stop_evt = threading.Event()
    state['logcat_stop'] = stop_evt
    t = threading.Thread(target=run_logcat, args=(serial, pid, stop_evt), daemon=True)
    t.start()


# ── Device monitor ──────────────────────────────────────────────────────────

def device_monitor():
    while True:
        serial = get_device()

        if serial != state['device_serial']:
            state['device_serial'] = serial
            state['device_connected'] = serial is not None
            state['pid'] = None
            if not serial:
                stop_logcat()
            socketio.emit('device_status', {
                'connected': serial is not None,
                'serial': serial,
                'pid': None,
            })

        if serial:
            pid = get_pid(serial)
            if pid != state['pid']:
                state['pid'] = pid
                socketio.emit('device_status', {
                    'connected': True, 'serial': serial, 'pid': pid,
                })
                if pid:
                    restart_logcat(serial, pid)
                else:
                    stop_logcat()

        time.sleep(2)


# ── Claude explanation ───────────────────────────────────────────────────────

def explain_log_with_claude(log: dict) -> str:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

        level_name = {'E': 'Error', 'W': 'Warning', 'I': 'Info', 'D': 'Debug', 'V': 'Verbose'}.get(log.get('level', ''), 'Unknown')

        prompt = (
            f"You are a mobile QA expert helping analyze Android logcat logs from "
            f"\"Merge Cruise\", a Unity-based mobile game by PeerPlay.\n\n"
            f"Analyze this log entry:\n"
            f"  Level:    {level_name} ({log.get('level')})\n"
            f"  Tag:      {log.get('tag')}\n"
            f"  Category: {log.get('category')}\n"
            f"  Time:     {log.get('timestamp')}\n"
            f"  Message:  {log.get('message')}\n\n"
            f"Respond in this exact format:\n"
            f"**What it means:** (1-2 sentences)\n"
            f"**Likely cause:** (1-2 sentences)\n"
            f"**Action needed:** (None / Monitor / Investigate / Fix immediately)\n\n"
            f"Be concise and practical. Avoid generic advice."
        )

        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return response.content[0].text

    except Exception as e:
        return f'Could not get explanation: {e}'


# ── Flask routes & Socket events ────────────────────────────────────────────

@app.route('/')
def index():
    from flask import make_response
    resp = make_response(render_template('index.html'))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/api/status')
def api_status():
    """REST fallback so the browser can always get device state reliably."""
    from flask import jsonify
    serial = get_device()
    pid    = get_pid(serial) if serial else None

    # Keep server state in sync
    state['device_serial']    = serial
    state['device_connected'] = serial is not None
    state['pid']              = pid

    if serial and pid and not state.get('logcat_proc'):
        restart_logcat(serial, pid)

    return jsonify({'connected': serial is not None, 'serial': serial, 'pid': pid})


@socketio.on('connect')
def on_connect():
    # Always do a fresh ADB poll on connect — don't rely on background thread timing
    serial = get_device()
    pid = get_pid(serial) if serial else None

    state['device_serial'] = serial
    state['device_connected'] = serial is not None
    state['pid'] = pid

    if serial and pid and not state.get('logcat_proc'):
        restart_logcat(serial, pid)

    with state['lock']:
        batch = state['log_buffer'][-500:]
    emit('log_batch', batch)
    emit('device_status', {
        'connected': serial is not None,
        'serial': serial,
        'pid': pid,
    })


@socketio.on('refresh_device')
def on_refresh():
    serial = get_device()
    pid = get_pid(serial) if serial else None

    state['device_serial'] = serial
    state['device_connected'] = serial is not None
    state['pid'] = pid

    if serial and pid:
        restart_logcat(serial, pid)
    else:
        stop_logcat()

    emit('device_status', {
        'connected': serial is not None,
        'serial': serial,
        'pid': pid,
    })


@socketio.on('clear_logs')
def on_clear():
    with state['lock']:
        state['log_buffer'].clear()
    socketio.emit('logs_cleared')


@socketio.on('explain_log')
def on_explain(log_data):
    explanation = explain_log_with_claude(log_data)
    emit('log_explanation', {'explanation': explanation})


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    t = threading.Thread(target=device_monitor, daemon=True)
    t.start()

    print('\n  ⚓  Merge Cruise Logcat Server — PeerPlay DevTools')
    print('  🌊  http://localhost:5001\n')

    socketio.run(app, host='0.0.0.0', port=5001, debug=False,
                 use_reloader=False, allow_unsafe_werkzeug=True)
