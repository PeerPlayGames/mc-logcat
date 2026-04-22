#!/usr/bin/env python3
"""
Merge Cruise Logcat Viewer — PeerPlay DevTools
"""

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit

# ── Load .env file if present (for ANTHROPIC_API_KEY etc.) ──────────────────
_env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

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

# ── Network traffic proxy state ──────────────────────────────────────────────

charles = {
    'entries_buffer': [],       # last 2000 entries in memory
    'lock': threading.Lock(),
}

proxy_state = {
    'running': False,
    'port': 8082,
    'master': None,
}

import queue as _queue_mod
_proxy_emit_queue = _queue_mod.Queue()

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


# ── Embedded HTTP/HTTPS proxy (mitmproxy) ────────────────────────────────────

def _proxy_status_cat(code: int) -> str:
    if code == 0:    return 'pending'
    if code < 300:   return 'ok'
    if code < 400:   return 'redirect'
    if code < 500:   return 'client_error'
    return 'server_error'


def _proxy_emit_worker():
    """Drain the cross-thread queue and push to Socket.IO."""
    while True:
        try:
            entry = _proxy_emit_queue.get(timeout=0.2)
            with charles['lock']:
                charles['entries_buffer'].append(entry)
                if len(charles['entries_buffer']) > 2000:
                    charles['entries_buffer'] = charles['entries_buffer'][-2000:]
            socketio.emit('charles_entry', entry)
        except _queue_mod.Empty:
            pass
        except Exception:
            pass


def _run_proxy_thread(port: int):
    try:
        import asyncio
        from mitmproxy.options import Options
        from mitmproxy.tools.dump import DumpMaster
        from mitmproxy import http as mhttp
        from urllib.parse import urlparse

        def _safe_body(msg, max_len=4000):
            """Decode HTTP body to a clean UTF-8 string, treating binary as a placeholder."""
            raw = msg.content
            if not raw:
                return ''
            ct = msg.headers.get('content-type', '').lower()
            # Explicitly binary content types — don't attempt to decode
            if any(x in ct for x in ('image/', 'video/', 'audio/', 'octet-stream',
                                       'protobuf', 'grpc', 'wasm')):
                return f'[binary: {len(raw)} bytes]'
            # Decode bytes; use replace so bad bytes become \ufffd, not surrogates
            text = raw.decode('utf-8', errors='replace')
            # If majority of first 200 chars are non-printable → binary blob
            sample = text[:200]
            printable = sum(1 for c in sample if c.isprintable() or c in '\n\r\t ')
            if sample and printable / len(sample) < 0.6:
                return f'[binary: {len(raw)} bytes]'
            return text[:max_len]

        class TrafficAddon:
            def response(self, flow: mhttp.HTTPFlow) -> None:
                try:
                    url    = flow.request.pretty_url
                    parsed = urlparse(url)
                    t0     = getattr(flow.request,  'timestamp_start', 0) or 0
                    t1     = getattr(flow.response, 'timestamp_end',   0) or 0
                    dur    = round((t1 - t0) * 1000) if t1 > t0 else 0
                    status = flow.response.status_code

                    # Use request START time so timestamps align with logcat
                    ts_dt = datetime.fromtimestamp(t0) if t0 > 0 else datetime.now()
                    ts    = ts_dt.strftime('%m-%d %H:%M:%S.') + f'{ts_dt.microsecond // 1000:03d}'

                    entry = {
                        'ts':          ts,
                        'ts_epoch':    t0 if t0 > 0 else ts_dt.timestamp(),
                        'method':      flow.request.method,
                        'url':         url,
                        'host':        parsed.netloc,
                        'path':        parsed.path + (f'?{parsed.query}' if parsed.query else ''),
                        'status':      status,
                        'status_cat':  _proxy_status_cat(status),
                        'duration':    dur,
                        'size':        len(flow.response.content) if flow.response.content else 0,
                        'req_headers': [{'name': k, 'value': v} for k, v in flow.request.headers.items()],
                        'req_body':    _safe_body(flow.request),
                        'resp_headers':[{'name': k, 'value': v} for k, v in flow.response.headers.items()],
                        'resp_body':   _safe_body(flow.response),
                    }
                    _proxy_emit_queue.put(entry)
                except Exception:
                    pass

        async def _run():
            opts   = Options(listen_host='0.0.0.0', listen_port=port, ssl_insecure=True)
            master = DumpMaster(opts, with_termlog=False, with_dumper=False)
            master.addons.add(TrafficAddon())
            proxy_state['master'] = master
            await master.run()

        asyncio.run(_run())

    except ImportError:
        socketio.emit('sys_msg', {
            'text': 'mitmproxy not installed — run: pip3 install mitmproxy',
            'type': 'error'
        })
    except Exception as e:
        if proxy_state['running']:
            socketio.emit('sys_msg', {'text': f'Proxy error: {e}', 'type': 'error'})
    finally:
        proxy_state['running'] = False
        proxy_state['master']  = None
        socketio.emit('proxy_status', {'running': False, 'port': proxy_state['port']})


def start_proxy(port: int = 8082):
    stop_proxy()
    proxy_state['port']    = port
    proxy_state['running'] = True
    threading.Thread(target=_run_proxy_thread, args=(port,), daemon=True).start()


def stop_proxy():
    proxy_state['running'] = False
    master = proxy_state.get('master')
    if master:
        try:
            master.shutdown()
        except Exception:
            pass
    proxy_state['master'] = None


def get_local_ip() -> str:
    import socket as _sock
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        try: s.close()
        except: pass


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
    # Send buffered logs to the newly connected browser.
    # Device state is handled by /api/status REST polling — no emit here.
    with state['lock']:
        batch = state['log_buffer'][-500:]
    emit('log_batch', batch)


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


@socketio.on('start_proxy')
def on_start_proxy(data):
    port = int((data or {}).get('port', 8082))
    start_proxy(port)
    emit('proxy_status', {'running': True, 'port': port, 'local_ip': get_local_ip()})
    # Send buffered entries to this new client
    with charles['lock']:
        batch = list(charles['entries_buffer'][-200:])
    if batch:
        emit('charles_batch', batch)


@socketio.on('stop_proxy')
def on_stop_proxy():
    stop_proxy()
    emit('proxy_status', {'running': False, 'port': proxy_state['port'], 'local_ip': get_local_ip()})


@socketio.on('clear_charles')
def on_clear_charles():
    with charles['lock']:
        charles['entries_buffer'].clear()
    socketio.emit('charles_cleared')


@app.route('/api/proxy/status')
def api_proxy_status():
    cert_path = os.path.expanduser('~/.mitmproxy/mitmproxy-ca-cert.pem')
    return jsonify({
        'running':        proxy_state['running'],
        'port':           proxy_state['port'],
        'local_ip':       get_local_ip(),
        'count':          len(charles['entries_buffer']),
        'cert_available': os.path.exists(cert_path),
    })


@app.route('/api/proxy/cert')
def api_proxy_cert():
    cert_path = os.path.expanduser('~/.mitmproxy/mitmproxy-ca-cert.pem')
    if not os.path.exists(cert_path):
        return jsonify({'error': 'Start the proxy first to generate the cert'}), 404
    from flask import send_file
    return send_file(cert_path, as_attachment=True,
                     download_name='mitmproxy-ca-cert.pem',
                     mimetype='application/x-pem-file')


@socketio.on('explain_log')
def on_explain(log_data):
    explanation = explain_log_with_claude(log_data)
    emit('log_explanation', {'explanation': explanation})


@socketio.on('explain_request')
def on_explain_request(req):
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY'))

        def _extract_binary_strings(data: bytes, min_len=5):
            import re as _re
            return list(dict.fromkeys(  # dedupe preserving order
                s.decode() for s in _re.findall(rb'[ -~]{' + str(min_len).encode() + rb',}', data)
            ))

        def _decode_b64_deep(obj, depth=0):
            import base64 as _b64, re as _re
            if depth > 4: return obj
            if isinstance(obj, list):  return [_decode_b64_deep(v, depth+1) for v in obj]
            if isinstance(obj, dict):  return {k: _decode_b64_deep(v, depth+1) for k, v in obj.items()}
            if isinstance(obj, str) and len(obj) > 40 and _re.match(r'^[A-Za-z0-9+/\-_]+=*$', obj):
                try:
                    raw = _b64.b64decode(obj.replace('-','+').replace('_','/') + '==')
                    # Try JSON first
                    try: return _decode_b64_deep(json.loads(raw), depth+1)
                    except: pass
                    # Check for binary (control chars > 12% of first 200 bytes)
                    sample = raw[:200]
                    ctrl = sum(1 for b in sample if b < 0x20 and b not in (0x09, 0x0A, 0x0D))
                    if ctrl / max(len(sample), 1) > 0.12:
                        strs = _extract_binary_strings(raw)
                        if strs: return f'[binary {len(raw)}B — strings: {", ".join(repr(s) for s in strs)}]'
                        return f'[binary: {len(raw)} bytes]'
                    return f'[base64→text: {raw.decode("utf-8", errors="replace")}]'
                except: pass
            return obj

        def fmt_body(raw):
            if not raw: return ''
            safe = raw.encode('utf-8', errors='replace').decode('utf-8')
            try: return json.dumps(_decode_b64_deep(json.loads(safe)), indent=2)[:2000]
            except: return safe[:2000]

        req_body  = fmt_body(req.get('req_body', ''))
        resp_body = fmt_body(req.get('resp_body', ''))

        status_label = {
            'ok': 'Success (2xx)', 'redirect': 'Redirect (3xx)',
            'client_error': 'Client Error (4xx)', 'server_error': 'Server Error (5xx)',
        }.get(req.get('status_cat',''), str(req.get('status','')))

        prompt = (
            f"You are a mobile QA expert analyzing HTTP traffic from "
            f"\"Merge Cruise\", a Unity-based mobile game by PeerPlay.\n\n"
            f"Analyze this network request:\n"
            f"  Method:   {req.get('method')}\n"
            f"  URL:      {req.get('url')}\n"
            f"  Status:   {req.get('status')} — {status_label}\n"
            f"  Duration: {req.get('duration')}ms\n"
            f"  Size:     {req.get('size', 0)} bytes\n"
        )
        if req_body:
            prompt += f"\nRequest body:\n{req_body}\n"
        if resp_body:
            prompt += f"\nResponse body:\n{resp_body}\n"

        prompt += (
            "\nRespond in this exact format:\n"
            "**What this call does:** (what the endpoint is for — be specific about game feature)\n"
            "**What the response means:** (interpret status + key fields from the response body)\n"
            "**Action needed:** (None / Monitor / Investigate / Fix immediately — with reason)\n\n"
            "Be concise. If the response body contains error details, explain them specifically."
        )

        response = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=450,
            messages=[{'role': 'user', 'content': prompt}]
        )
        emit('log_explanation', {'explanation': response.content[0].text})
    except Exception as e:
        emit('log_explanation', {'explanation': f'Could not get explanation: {e}'})


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    threading.Thread(target=device_monitor, daemon=True).start()
    threading.Thread(target=_proxy_emit_worker, daemon=True).start()

    print('\n  ⚓  Merge Cruise Logcat Server — PeerPlay DevTools')
    print('  🌊  http://localhost:5001\n')

    socketio.run(app, host='0.0.0.0', port=5001, debug=False,
                 use_reloader=False, allow_unsafe_werkzeug=True)
