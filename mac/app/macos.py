"""macOS layer: find the game window, screenshot it, click/drag inside it, and check permissions.

Works with any window that shows Last Z: BlueStacks / BlueStacks Air, the iPhone/iPad app on Apple-silicon
Macs, or another emulator. Coordinates from Gemini are 0-1000 relative to the window and are mapped to
screen points here.
"""
import os
import random
import subprocess
import tempfile
import time

import Quartz
from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps

try:
    import ApplicationServices as AS
except ImportError:  # pragma: no cover
    AS = None

# Words that point to a window that is (or hosts) Last Z, best first.
GAME_HINTS = [
    ("last z", 100), ("lastz", 100), ("survival shooter", 90),
    ("bluestacks", 60), ("mumu", 50), ("ldplayer", 50), ("nox", 40), ("android", 30), ("emulator", 30),
]
IGNORE_OWNERS = {"Window Server", "Dock", "Control Center", "Notification Center", "Saad Shard Sniper",
                 "Spotlight", "SystemUIServer", "Finder"}


# ---------------------------------------------------------------- windows

def list_windows():
    """Visible normal windows big enough to be a game, as dicts."""
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    infos = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    out = []
    for w in infos:
        if w.get("kCGWindowLayer", 0) != 0:
            continue
        owner = str(w.get("kCGWindowOwnerName", "") or "")
        if owner in IGNORE_OWNERS:
            continue
        b = w.get("kCGWindowBounds", {}) or {}
        if b.get("Width", 0) < 240 or b.get("Height", 0) < 240:
            continue
        title = str(w.get("kCGWindowName", "") or "")
        out.append({
            "id": int(w["kCGWindowNumber"]),
            "pid": int(w["kCGWindowOwnerPID"]),
            "owner": owner,
            "title": title,
            "label": owner + (" — " + title if title and title != owner else ""),
            "score": _score(owner, title),
        })
    out.sort(key=lambda x: -x["score"])
    return out


def _score(owner, title):
    text = (owner + " " + title).lower()
    return max([s for k, s in GAME_HINTS if k in text] or [0])


def guess_game_window():
    """Best guess for the game window, or None."""
    wins = [w for w in list_windows() if w["score"] > 0]
    return wins[0] if wins else None


def window_info(win_id):
    infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionIncludingWindow, win_id) or []
    for w in infos:
        if int(w.get("kCGWindowNumber", -1)) == int(win_id):
            b = w.get("kCGWindowBounds", {}) or {}
            return {
                "id": int(win_id),
                "pid": int(w.get("kCGWindowOwnerPID", 0)),
                "x": float(b.get("X", 0)), "y": float(b.get("Y", 0)),
                "w": float(b.get("Width", 0)), "h": float(b.get("Height", 0)),
                "onscreen": bool(w.get("kCGWindowIsOnscreen", True)),
            }
    return None


def activate(pid):
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is not None:
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)


# ---------------------------------------------------------------- capture

def capture_jpeg(win_id, max_width=900):
    """Screenshot of just that window as JPEG bytes (needs Screen Recording permission)."""
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        r = subprocess.run(["screencapture", "-x", "-o", "-t", "jpg", "-l", str(int(win_id)), path],
                           capture_output=True, timeout=15)
        if r.returncode != 0 or not os.path.getsize(path):
            return None
        # shrink to keep Gemini fast and cheap
        subprocess.run(["sips", "--resampleWidth", str(max_width), path], capture_output=True, timeout=15)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def capture_full_jpeg(win_id):
    """Full-resolution window screenshot for the Discord alert."""
    return capture_jpeg(win_id, max_width=1400)


# ---------------------------------------------------------------- input

def _post(kind, x, y):
    e = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)


def to_screen(win, nx, ny):
    """0-1000 window coords -> global screen points, kept a little inside the edges."""
    nx = min(max(float(nx), 5), 995)
    ny = min(max(float(ny), 5), 995)
    return win["x"] + win["w"] * nx / 1000.0, win["y"] + win["h"] * ny / 1000.0


def click(x, y):
    x += random.uniform(-2, 2)
    y += random.uniform(-2, 2)
    _post(Quartz.kCGEventMouseMoved, x, y)
    time.sleep(0.05)
    _post(Quartz.kCGEventLeftMouseDown, x, y)
    time.sleep(random.uniform(0.06, 0.12))
    _post(Quartz.kCGEventLeftMouseUp, x, y)


def drag(x1, y1, x2, y2, steps=18):
    _post(Quartz.kCGEventMouseMoved, x1, y1)
    time.sleep(0.05)
    _post(Quartz.kCGEventLeftMouseDown, x1, y1)
    for i in range(1, steps + 1):
        t = i / steps
        _post(Quartz.kCGEventLeftMouseDragged, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
        time.sleep(0.02)
    time.sleep(0.05)
    _post(Quartz.kCGEventLeftMouseUp, x2, y2)


def press_escape():
    """'Back' in emulators (Esc maps to Android back in BlueStacks)."""
    for down in (True, False):
        e = Quartz.CGEventCreateKeyboardEvent(None, 53, down)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e)
        time.sleep(0.04)


def mouse_position():
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return loc.x, loc.y


# ---------------------------------------------------------------- permissions

def has_screen_recording():
    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except AttributeError:
        return True


def ask_screen_recording():
    try:
        Quartz.CGRequestScreenCaptureAccess()
    except AttributeError:
        pass
    open_settings("Privacy_ScreenCapture")


def has_accessibility():
    if AS is None:
        return True
    return bool(AS.AXIsProcessTrusted())


def ask_accessibility():
    if AS is not None:
        AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})
    open_settings("Privacy_Accessibility")


def open_settings(pane):
    subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?" + pane])


def open_url(url):
    subprocess.run(["open", url])
