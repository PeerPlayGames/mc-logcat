#!/usr/bin/env python3
"""
UIAutomator-based Wi-Fi proxy setter/clearer for Samsung Android devices.
Usage:
  python3 device_proxy.py set <ip> <port> [pin]
  python3 device_proxy.py clear [pin]
"""
import subprocess, time, sys, re


# ── Helpers ──────────────────────────────────────────────────────────────────

def adb(cmd):
    r = subprocess.run(["adb", "shell"] + cmd.split(),
                       capture_output=True, text=True, timeout=10)
    return r.stdout

def tap(x, y, wait=1.5):
    adb(f"input tap {x} {y}")
    time.sleep(wait)

def swipe(y1, y2, dur=300, wait=1.0):
    adb(f"input swipe 540 {y1} 540 {y2} {dur}")
    time.sleep(wait)

def dump():
    adb("uiautomator dump /sdcard/ui_auto.xml")
    time.sleep(0.3)
    return adb("cat /sdcard/ui_auto.xml")

def texts(xml):
    return set(re.findall(r'text="([^"]+)"', xml))

def bounds_of(xml, text_match):
    pat = f'text="{re.escape(text_match)}"[^>]*bounds="\\[([0-9]+),([0-9]+)\\]\\[([0-9]+),([0-9]+)\\]"'
    m = re.search(pat, xml)
    if m:
        x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return (x1 + x2) // 2, (y1 + y2) // 2
    return None

def bounds_of_desc(xml, desc):
    pat = f'content-desc="{re.escape(desc)}"[^>]*bounds="\\[([0-9]+),([0-9]+)\\]\\[([0-9]+),([0-9]+)\\]"'
    m = re.search(pat, xml)
    if m:
        x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return (x1 + x2) // 2, (y1 + y2) // 2
    return None

def checked_text_bounds(xml, text_match):
    """Find bounds specifically in a CheckedTextView (dropdown items)."""
    pat = (f'text="{re.escape(text_match)}"[^>]*class="android.widget.CheckedTextView"'
           f'[^>]*bounds="\\[([0-9]+),([0-9]+)\\]\\[([0-9]+),([0-9]+)\\]"')
    m = re.search(pat, xml)
    if m:
        x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return (x1 + x2) // 2, (y1 + y2) // 2
    return None


# ── Unlock / PIN ─────────────────────────────────────────────────────────────

def unlock(pin):
    wake = adb("dumpsys power")
    if "mWakefulness=Awake" in wake:
        lock = adb("dumpsys window")
        if "mDreamingLockscreen=true" not in lock:
            return
    adb("input keyevent 26")
    time.sleep(1)
    adb("input swipe 540 1800 540 800 300")
    time.sleep(1)
    if pin:
        enter_pin(pin)

def enter_pin(pin):
    for ch in pin:
        adb(f"input text {ch}")
        time.sleep(0.1)
    time.sleep(0.3)
    adb("input keyevent 66")
    time.sleep(2)


# ── Automation cleanup ───────────────────────────────────────────────────────

def cleanup_automation():
    """Kill all traces of UIAutomator and Settings to prevent ghost automation."""
    adb("am force-stop com.android.settings")
    # Kill UIAutomator processes
    subprocess.run(["adb", "shell", "pkill", "-f", "uiautomator"], capture_output=True, timeout=5)
    # Remove dump file
    subprocess.run(["adb", "shell", "rm", "-f", "/sdcard/ui_auto.xml"], capture_output=True, timeout=5)
    # Reset accessibility to flush any lingering UIAutomator connection
    subprocess.run(["adb", "shell", "settings", "put", "secure", "accessibility_enabled", "0"],
                   capture_output=True, timeout=5)
    # Kill the accessibility service cache
    subprocess.run(["adb", "shell", "am", "broadcast",
                    "-a", "com.android.server.accessibility.AccessibilityManagerService"],
                   capture_output=True, timeout=5)
    # Temporarily disable and re-enable the game to clear all pending intents
    subprocess.run(["adb", "shell", "pm", "disable", "com.peerplay.megamerge"],
                   capture_output=True, timeout=5)
    time.sleep(0.5)
    subprocess.run(["adb", "shell", "pm", "enable", "com.peerplay.megamerge"],
                   capture_output=True, timeout=5)


# ── Navigation helpers ───────────────────────────────────────────────────────

def open_wifi():
    adb("am start -a android.settings.WIFI_SETTINGS --activity-brought-to-front")
    time.sleep(2)

def ensure_settings(retries=2):
    """Make sure Settings is in foreground; reopen if another app stole focus."""
    xml = dump()
    if "com.android.settings" in xml:
        return xml
    if retries <= 0:
        print("WARNING: Could not bring Settings to foreground")
        return xml
    open_wifi()
    time.sleep(1)
    return ensure_settings(retries - 1)

def navigate_to_proxy_dropdown(wifi_name, pin):
    """Navigate from Wi-Fi list to the Proxy dropdown. Returns xml with dropdown showing."""
    xml = ensure_settings()
    t = texts(xml)

    # If on Wi-Fi list, tap gear
    if f"{wifi_name} Settings Button" in xml:
        pos = bounds_of_desc(xml, f"{wifi_name} Settings Button")
        if pos:
            tap(*pos)
            xml = ensure_settings()
            t = texts(xml)

    # If View more is visible, tap it
    if "View more" in t:
        pos = bounds_of(xml, "View more")
        if pos:
            tap(*pos)
            xml = dump()
            t = texts(xml)
            if "Confirm PIN" in t:
                print("  PIN required...")
                enter_pin(pin)
                open_wifi()
                return navigate_to_proxy_dropdown(wifi_name, pin)

    xml = ensure_settings()
    t = texts(xml)

    # Scroll to Proxy if not visible
    if "Proxy" not in t:
        swipe(1800, 1200)
        xml = ensure_settings()
        t = texts(xml)
    if "Proxy" not in t:
        swipe(1800, 1000)
        xml = ensure_settings()
        t = texts(xml)
    if "Proxy" not in t:
        print("ERROR: Proxy row not found")
        sys.exit(1)

    # Tap Proxy
    pos = bounds_of(xml, "Proxy")
    tap(*pos)
    xml = dump()
    t = texts(xml)

    # Handle PIN again
    if "Confirm PIN" in t:
        print("  PIN required...")
        enter_pin(pin)
        open_wifi()
        return navigate_to_proxy_dropdown(wifi_name, pin)

    return xml


def find_wifi_name(xml):
    """Find the connected network name from a Wi-Fi settings dump."""
    for name in re.findall(r'text="([^"]+)"', xml):
        if f'{name} Settings Button' in xml:
            return name
    return None


# ── SET proxy ────────────────────────────────────────────────────────────────

def set_proxy(ip, port, pin):
    print("Step 0: Unlock + close game")
    unlock(pin)
    adb("am force-stop com.peerplay.megamerge")
    time.sleep(0.5)

    print("Step 1: Open Wi-Fi settings")
    open_wifi()
    xml = ensure_settings()

    wifi_name = find_wifi_name(xml)
    if not wifi_name:
        print("ERROR: No connected Wi-Fi network found")
        sys.exit(1)
    print(f"  Network: {wifi_name}")

    print("Step 2-4: Navigate to Proxy dropdown")
    xml = navigate_to_proxy_dropdown(wifi_name, pin)
    t = texts(xml)

    if "Manual" not in t or "Auto-config" not in t:
        print("ERROR: Proxy dropdown not showing")
        sys.exit(1)

    print("Step 5: Select 'Manual'")
    pos = checked_text_bounds(xml, "Manual") or bounds_of(xml, "Manual")
    tap(*pos)
    xml = ensure_settings()

    print("Step 6: Scroll to proxy fields")
    swipe(1800, 600)
    xml = ensure_settings()

    print("Step 7: Fill hostname")
    pos = bounds_of(xml, "proxy.example.com")
    if not pos:
        pos = bounds_of(xml, "Proxy host name")
        if pos:
            tap(pos[0], pos[1] + 60, wait=0.5)
        else:
            print("ERROR: Hostname field not found")
            sys.exit(1)
    else:
        tap(*pos, wait=0.5)
    adb(f"input text {ip}")
    time.sleep(0.5)

    print("Step 8: Fill port")
    xml = dump()
    pos = bounds_of(xml, "8080")
    if not pos:
        pos = bounds_of(xml, "Proxy port")
        if pos:
            tap(pos[0], pos[1] + 60, wait=0.3)
        else:
            print("ERROR: Port field not found")
            sys.exit(1)
    else:
        tap(*pos, wait=0.3)
    for _ in range(4):
        adb("input keyevent 67")
    time.sleep(0.2)
    adb(f"input text {port}")
    time.sleep(0.5)

    print("Step 9: Save")
    adb("input keyevent 111")
    time.sleep(0.5)
    xml = dump()
    pos = bounds_of(xml, "Save")
    if not pos:
        swipe(1800, 1200)
        xml = dump()
        pos = bounds_of(xml, "Save")
    if pos:
        tap(*pos)
        print(f"DONE: Proxy set to {ip}:{port}")
    else:
        print("WARNING: Save button not found")

    print("Step 10: Cleanup and launch game")
    cleanup_automation()
    time.sleep(0.5)
    adb("input keyevent 3")
    time.sleep(0.5)
    adb("am start -n com.peerplay.megamerge/com.unity3d.player.UnityPlayerActivity")


# ── CLEAR proxy ──────────────────────────────────────────────────────────────

def clear_proxy(pin):
    print("Step 0: Unlock + close game")
    unlock(pin)
    adb("am force-stop com.peerplay.megamerge")
    time.sleep(0.5)

    print("Step 1: Open Wi-Fi settings")
    open_wifi()
    xml = ensure_settings()

    wifi_name = find_wifi_name(xml)
    if not wifi_name:
        # Maybe already on network detail page
        t = texts(xml)
        for candidate in ("GamingHub", "Krypton"):
            if candidate in t:
                wifi_name = candidate
                break
    if not wifi_name:
        print("ERROR: No Wi-Fi network found")
        sys.exit(1)
    print(f"  Network: {wifi_name}")

    print("Step 2-4: Navigate to Proxy dropdown")
    xml = navigate_to_proxy_dropdown(wifi_name, pin)
    t = texts(xml)

    if "None" not in t:
        print("ERROR: Dropdown not showing 'None'")
        sys.exit(1)

    print("Step 5: Select 'None'")
    pos = checked_text_bounds(xml, "None") or bounds_of(xml, "None")
    if not pos:
        print("ERROR: None option not found")
        sys.exit(1)
    tap(*pos)

    print("Step 6: Save")
    time.sleep(1)
    xml = ensure_settings()
    pos = bounds_of(xml, "Save")
    if not pos:
        swipe(1800, 1200)
        xml = ensure_settings()
        pos = bounds_of(xml, "Save")
    if pos:
        tap(*pos)
        print("DONE: Proxy cleared (set to None)")
    else:
        print("DONE: Proxy cleared (auto-saved)")

    cleanup_automation()
    time.sleep(0.5)
    adb("input keyevent 3")


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: device_proxy.py set <ip> <port> [pin]")
        print("       device_proxy.py clear [pin]")
        sys.exit(1)

    action = sys.argv[1]
    if action == 'set':
        ip   = sys.argv[2] if len(sys.argv) > 2 else "192.168.1.227"
        port = sys.argv[3] if len(sys.argv) > 3 else "8082"
        pin  = sys.argv[4] if len(sys.argv) > 4 else ""
        set_proxy(ip, port, pin)
    elif action == 'clear':
        pin = sys.argv[2] if len(sys.argv) > 2 else ""
        clear_proxy(pin)
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)
