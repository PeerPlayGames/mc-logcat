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
    'loop':   None,
    'thread': None,
}
# Set when the proxy thread has fully exited and the port is released.
# Pre-set so the first start_proxy doesn't wait.
_proxy_stopped = threading.Event()
_proxy_stopped.set()

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
        # If first message contains JSON (analytics/structured logs), join without
        # newlines so the JSON stays parseable.  Otherwise keep newline-separated.
        first_msg = first['message']
        if '{' in first_msg or '[' in first_msg:
            merged['message'] = first_msg + ''.join(extras)
        else:
            merged['message'] = first_msg + '\n' + '\n'.join(extras)
        merged['category'] = categorise(first['tag'], merged['message'])
    store_and_emit_fn(merged)


# Regex to detect a line that starts a NEW Unity log (has a [Tag] prefix or looks
# like a fresh log).  Continuation lines from a split message won't match.
_RE_NEW_UNITY_MSG = re.compile(r'^\[[\w]+\]|^------|^#\d|^System\.')


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
        group_tid = None      # tid of current group

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
                tid = parsed['tid']
                msg = parsed['message']
                # Same tid AND message looks like a continuation (no [Tag] prefix)
                # → merge into current group (logcat splits long messages)
                if tid == group_tid and unity_group and not _RE_NEW_UNITY_MSG.match(msg):
                    unity_group.append(parsed)
                else:
                    # New log message — flush previous group
                    flush_unity_group(unity_group, store_and_emit)
                    unity_group = [parsed]
                    group_tid = tid
            else:
                # Non-Unity line — flush any pending Unity group first
                flush_unity_group(unity_group, store_and_emit)
                unity_group = []
                group_tid = None
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


# ── MessagePack / game-state decoders ────────────────────────────────────────

_CURRENCY_TYPES  = {0: 'MetaPoints', 1: 'Credits', 2: 'Flowers', 3: 'Stars', 5: 'Event', 10000: 'None'}
_OPERATION_TYPES = {0: 'None', 1: 'Add', 2: 'Subtract', 3: 'Set'}


def _decode_balance_history_entry(arr):
    """Map a BalanceHistoryData positional array to a named dict."""
    if not isinstance(arr, (list, tuple)) or len(arr) < 6:
        return arr
    return {
        'id':            arr[0],
        'operation':     _OPERATION_TYPES.get(arr[1], arr[1]),
        'amount':        arr[2],
        'balance_after': arr[3],
        'reason':        arr[4],
        'timestamp':     str(arr[5]),
    }


def _decode_balance_data(raw_bytes):
    """Decode a BalanceData MessagePack positional array → readable dict."""
    try:
        import msgpack
        arr = msgpack.unpackb(raw_bytes, raw=False, strict_map_key=False)
        if not isinstance(arr, (list, tuple)):
            return {'_raw': repr(arr)}
        result = {}
        # Key(6): RealCurrencies {int → int}
        if len(arr) > 6 and isinstance(arr[6], dict):
            result['balances'] = {
                _CURRENCY_TYPES.get(k, f'currency_{k}'): v
                for k, v in arr[6].items()
            }
        # Key(7): CurrenciesHistory {int → [BalanceHistoryData]}
        if len(arr) > 7 and isinstance(arr[7], dict):
            history = {}
            for k, entries in arr[7].items():
                cname = _CURRENCY_TYPES.get(k, f'currency_{k}')
                if isinstance(entries, list):
                    history[cname] = [_decode_balance_history_entry(e) for e in entries]
                else:
                    history[cname] = entries
            if history:
                result['transaction_history'] = history
        if len(arr) > 3 and arr[3] is not None:
            result['successful_purchases'] = arr[3]
        if len(arr) > 8 and arr[8] is not None:
            result['last_sync_timestamp'] = str(arr[8])
        return result
    except Exception as e:
        return {'_decode_error': str(e), '_raw_len': len(raw_bytes)}


def _decode_b64_msgpack_value(key, value):
    """Decode a single Base64-encoded MessagePack value given its key name."""
    import base64 as _b64
    if not isinstance(value, str):
        return value
    try:
        padded = value.replace('-', '+').replace('_', '/')
        raw = _b64.b64decode(padded + '==')
    except Exception:
        return value
    if key == 'BalanceData':
        return _decode_balance_data(raw)
    try:
        import msgpack
        return msgpack.unpackb(raw, raw=False, strict_map_key=False)
    except Exception:
        return f'[binary: {len(raw)} bytes]'


def _decode_state_update_body(body_text):
    """
    Decode a /state/update request body.
    Format: {"items": [{"key": "TypeName", "value": "<base64 msgpack>"}]}
    Returns formatted JSON string, or None if not applicable.
    """
    try:
        obj = json.loads(body_text)
        items = obj.get('items')
        if not isinstance(items, list):
            return None
        decoded = {}
        for item in items:
            key   = item.get('key', '')
            value = item.get('value', '')
            decoded[key] = _decode_b64_msgpack_value(key, value)
        return json.dumps(decoded, indent=2, default=str)
    except Exception:
        return None


def _decode_checkpoint_body(body_text):
    """
    Decode a /create-checkpoint (or similar) request body.
    Format: {"state": {"BalanceData": "<base64 msgpack>", ...}, ...}
    Returns formatted JSON string, or None if not applicable.
    """
    try:
        obj = json.loads(body_text)
        state = obj.get('state')
        if not isinstance(state, dict):
            return None
        decoded_state = {}
        for key, value in state.items():
            decoded_state[key] = _decode_b64_msgpack_value(key, value)
        result = {k: v for k, v in obj.items() if k != 'state'}
        result['state'] = decoded_state
        return json.dumps(result, indent=2, default=str)
    except Exception:
        return None


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

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        proxy_state['loop'] = loop

        def _loop_exception_handler(lp, context):
            exc = context.get('exception')
            # Suppress the benign "Event loop is closed" error that mitmproxy
            # raises internally when we shut down the loop from another thread.
            if isinstance(exc, RuntimeError) and 'Event loop is closed' in str(exc):
                return
            lp.default_exception_handler(context)
        loop.set_exception_handler(_loop_exception_handler)

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

        def _get_req_body(req_msg, path: str) -> str:
            """Return request body, with MessagePack decoding for /state/update,
            /create-checkpoint, and URL-form decoding for Mixpanel /track requests."""
            raw_text = _safe_body(req_msg, max_len=20000)
            if '/state/update' in path and raw_text and not raw_text.startswith('[binary'):
                decoded = _decode_state_update_body(raw_text)
                if decoded is not None:
                    return decoded
            # Decode any game server body with {"state": {"Key": "<base64 msgpack>", ...}}
            # (e.g. /create-checkpoint, /load-checkpoint, etc.)
            if raw_text and not raw_text.startswith('[binary') and '"state"' in raw_text:
                decoded = _decode_checkpoint_body(raw_text)
                if decoded is not None:
                    return decoded
            # Decode Mixpanel URL-form-encoded body: data=<url-encoded-json>
            if '/track' in path and raw_text and 'data=' in raw_text:
                try:
                    from urllib.parse import parse_qs
                    params = parse_qs(raw_text, keep_blank_values=True)
                    data_vals = params.get('data', [])
                    if data_vals:
                        return json.dumps(json.loads(data_vals[0]), indent=2)
                except Exception:
                    pass
            return raw_text

        def _get_resp_body(resp_msg, path: str) -> str:
            """Return response body, with state-dict decoding for game server responses."""
            raw_text = _safe_body(resp_msg)
            if raw_text and not raw_text.startswith('[binary') and '"state"' in raw_text:
                decoded = _decode_checkpoint_body(raw_text)
                if decoded is not None:
                    return decoded
            if '/state/update' in path and raw_text and not raw_text.startswith('[binary'):
                decoded = _decode_state_update_body(raw_text)
                if decoded is not None:
                    return decoded
            return raw_text

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
                        'req_body':    _get_req_body(flow.request, parsed.path),
                        'resp_headers':[{'name': k, 'value': v} for k, v in flow.response.headers.items()],
                        'resp_body':   _get_resp_body(flow.response, parsed.path),
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

        loop.run_until_complete(_run())

    except ImportError:
        socketio.emit('sys_msg', {
            'text': 'mitmproxy not installed — run: pip3 install mitmproxy',
            'type': 'error'
        })
    except Exception as e:
        if proxy_state['running']:
            socketio.emit('sys_msg', {'text': f'Proxy error: {e}', 'type': 'error'})
    finally:
        print('[proxy] thread finally: closing loop...')
        try:
            loop = proxy_state.get('loop')
            if loop and not loop.is_closed():
                loop.close()
        except Exception:
            pass
        proxy_state['master'] = None
        proxy_state['loop']   = None
        print('[proxy] signalling _proxy_stopped')
        _proxy_stopped.set()
        # Only broadcast "stopped" if no new proxy has already been started
        # (avoids clobbering the running:True emitted by a restart)
        if not proxy_state['running']:
            socketio.emit('proxy_status', {'running': False, 'port': proxy_state['port']})


def start_proxy(port: int = 8082):
    print(f'[proxy] start_proxy called (port={port})')
    old_thread = proxy_state.get('thread')
    old_alive  = old_thread is not None and old_thread.is_alive()

    if old_alive:
        # Clear the event BEFORE stopping so we can wait on it below
        _proxy_stopped.clear()
        print('[proxy] clearing _proxy_stopped, calling stop_proxy...')

    stop_proxy()

    if old_alive:
        print('[proxy] waiting for old proxy thread to fully exit (timeout=8s)...')
        if _proxy_stopped.wait(timeout=8):
            print('[proxy] old proxy stopped cleanly')
        else:
            print('[proxy] WARNING: timed out waiting for old proxy — proceeding anyway')
        # Give the OS a moment to release the socket from TIME_WAIT
        time.sleep(0.4)
        print('[proxy] port wait done')
    else:
        # No prior thread running — ensure event is set so next stop can clear it
        _proxy_stopped.set()

    proxy_state['port']    = port
    proxy_state['running'] = True
    t = threading.Thread(target=_run_proxy_thread, args=(port,), daemon=True)
    proxy_state['thread'] = t
    print('[proxy] starting new proxy thread...')
    t.start()


def stop_proxy():
    print('[proxy] stop_proxy called')
    proxy_state['running'] = False
    master = proxy_state.get('master')
    loop   = proxy_state.get('loop')
    if loop and loop.is_running():
        import asyncio as _aio

        async def _shutdown_coro():
            # THE FIX: explicitly stop server instances via proxyserver addon.
            # master.shutdown() only sets should_exit — it does NOT close the TCP
            # listener socket.  servers.update([]) calls ServerInstance.stop() on
            # each bound port, which is the only call that actually releases the port.
            if master:
                try:
                    ps = master.addons.get("proxyserver")
                    if ps and hasattr(ps, "servers"):
                        await ps.servers.update([])
                        print("[proxy] servers.update([]) done — port released")
                except Exception as ex:
                    print(f"[proxy] servers.update error: {ex}")
                try:
                    master.shutdown()
                except Exception:
                    pass
            loop.stop()

        print('[proxy] scheduling shutdown coroutine...')
        try:
            _aio.run_coroutine_threadsafe(_shutdown_coro(), loop)
        except Exception as ex:
            print(f'[proxy] could not schedule shutdown ({ex}) — force stopping loop')
            loop.call_soon_threadsafe(loop.stop)
    elif master:
        print('[proxy] no running loop — calling master.shutdown() directly')
        try:
            master.shutdown()
        except Exception:
            pass
    else:
        print('[proxy] nothing to stop')
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


# ── Claude explanation (via Claude Code CLI — no separate API key needed) ────

def _run_claude(prompt: str, timeout: int = 30) -> str:
    """Run a prompt through the Claude Code CLI (uses existing subscription)."""
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(prompt)
            prompt_file = f.name
        result = subprocess.run(
            ['claude', '-p', '--model', 'haiku', prompt],
            capture_output=True, text=True, timeout=timeout
        )
        try:
            os.unlink(prompt_file)
        except Exception:
            pass
        if result.returncode != 0:
            err = result.stderr.strip() or 'Unknown error'
            return f'⚠ Claude CLI error: {err[:400]}'
        return result.stdout.strip()
    except FileNotFoundError:
        return '⚠ Claude CLI not found — install Claude Code: npm install -g @anthropic-ai/claude-code'
    except subprocess.TimeoutExpired:
        return '⚠ Claude timed out after 30s'
    except Exception as e:
        return f'⚠ Claude error: {str(e)[:400]}'


def explain_log_with_claude(log: dict) -> str:
    level_name = {'E': 'Error', 'W': 'Warning', 'I': 'Info', 'D': 'Debug', 'V': 'Verbose'}.get(log.get('level', ''), 'Unknown')
    msg = (log.get('message') or '')[:800]

    prompt = (
        f"You are a mobile QA expert analyzing Android logcat from "
        f"\"Merge Cruise\", a Unity mobile game by PeerPlay.\n\n"
        f"Log entry:\n"
        f"  Level:    {level_name} ({log.get('level')})\n"
        f"  Tag:      {log.get('tag')}\n"
        f"  Category: {log.get('category')}\n"
        f"  Time:     {log.get('timestamp')}\n"
        f"  Message:  {msg}\n\n"
        f"Explain this in plain English for a QA tester. Respond with:\n"
        f"**What it means:** (what this log event indicates in the game)\n"
        f"**Likely cause:** (what triggered it)\n"
        f"**Action needed:** None / Monitor / Investigate / Fix immediately — and why\n\n"
        f"Be specific to Merge Cruise. Avoid generic advice."
    )
    return _run_claude(prompt)


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


# ── Device proxy (UIAutomator-based Wi-Fi proxy set/clear) ──────────────────

_DEVICE_PROXY_SCRIPT = os.path.join(os.path.dirname(__file__), 'device_proxy.py')


@app.route('/api/proxy/set-device', methods=['POST'])
def api_proxy_set_device():
    from flask import request as _req
    ip   = _req.json.get('ip', get_local_ip())
    port = str(_req.json.get('port', proxy_state['port']))
    pin  = _req.json.get('pin', '')

    args = ['python3', _DEVICE_PROXY_SCRIPT, 'set', ip, port]
    if pin:
        args.append(pin)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0
        return jsonify({'ok': ok, 'output': r.stdout + r.stderr})
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False, 'output': 'Timeout (120s)'}), 504
    except Exception as ex:
        return jsonify({'ok': False, 'output': str(ex)}), 500


@app.route('/api/proxy/clear-device', methods=['POST'])
def api_proxy_clear_device():
    from flask import request as _req
    pin = _req.json.get('pin', '')

    args = ['python3', _DEVICE_PROXY_SCRIPT, 'clear']
    if pin:
        args.append(pin)
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0
        return jsonify({'ok': ok, 'output': r.stdout + r.stderr})
    except subprocess.TimeoutExpired:
        return jsonify({'ok': False, 'output': 'Timeout (120s)'}), 504
    except Exception as ex:
        return jsonify({'ok': False, 'output': str(ex)}), 500


@socketio.on('explain_log')
def on_explain(log_data):
    explanation = explain_log_with_claude(log_data)
    emit('log_explanation', {'explanation': explanation})


@socketio.on('explain_request')
def on_explain_request(req):
    def _fmt_body(raw):
        if not raw: return ''
        safe = raw.encode('utf-8', errors='replace').decode('utf-8')
        try: return json.dumps(json.loads(safe), indent=2)[:2000]
        except: return safe[:2000]

    req_body  = _fmt_body(req.get('req_body', ''))[:600]
    resp_body = _fmt_body(req.get('resp_body', ''))[:600]

    status_label = {
        'ok': 'Success (2xx)', 'redirect': 'Redirect (3xx)',
        'client_error': 'Client Error (4xx)', 'server_error': 'Server Error (5xx)',
    }.get(req.get('status_cat',''), str(req.get('status','')))

    prompt = (
        f"You are a mobile QA expert analyzing HTTP traffic from "
        f"\"Merge Cruise\", a Unity mobile game by PeerPlay.\n\n"
        f"Network request:\n"
        f"  Method:   {req.get('method')}\n"
        f"  URL:      {req.get('url')}\n"
        f"  Status:   {req.get('status')} — {status_label}\n"
        f"  Duration: {req.get('duration')}ms\n"
        f"  Size:     {req.get('size', 0)} bytes\n"
    )
    if req_body:
        prompt += f"\nRequest body (truncated):\n{req_body}\n"
    if resp_body:
        prompt += f"\nResponse body (truncated):\n{resp_body}\n"

    prompt += (
        "\nExplain this in plain English for a QA tester. Respond with:\n"
        "**What this call does:** (what game feature/system this endpoint serves)\n"
        "**What the data shows:** (interpret the key fields — balances, status codes, errors)\n"
        "**Action needed:** None / Monitor / Investigate / Fix immediately — and why\n\n"
        "Be specific to Merge Cruise. If there are errors or anomalies in the data, explain exactly what they mean."
    )

    explanation = _run_claude(prompt)
    emit('log_explanation', {'explanation': explanation})


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    threading.Thread(target=device_monitor, daemon=True).start()
    threading.Thread(target=_proxy_emit_worker, daemon=True).start()

    print('\n  ⚓  Merge Cruise Logcat Server — PeerPlay DevTools')
    print('  🌊  http://localhost:5001\n')

    socketio.run(app, host='0.0.0.0', port=5001, debug=False,
                 use_reloader=False, allow_unsafe_werkzeug=True)
