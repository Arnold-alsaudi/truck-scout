"""Windows layer (same functions as macos.py): find the game window, screenshot it, click/drag inside it.

Works with BlueStacks / BlueStacks 5, LDPlayer, MuMu, Nox or any window that shows Last Z.
Coordinates from Gemini are 0-1000 relative to the window's client area and are mapped to screen pixels here.
"""
import ctypes
import ctypes.wintypes as wt
import io
import os
import random
import time

from PIL import Image, ImageGrab

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# real pixels everywhere (no DPI scaling surprises)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass

GAME_HINTS = [
    ("last z", 100), ("lastz", 100), ("survival shooter", 90),
    ("bluestacks", 60), ("hd-player", 60), ("mumu", 50), ("ldplayer", 50), ("nox", 40), ("memu", 40),
    ("android", 30), ("emulator", 30),
]
IGNORE_TITLES = {"Saad Shard Sniper", "Program Manager", "Settings", "Microsoft Text Input Application"}


# ---------------------------------------------------------------- windows

def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _proc_name(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    name = ""
    if h:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            name = os.path.splitext(os.path.basename(buf.value))[0]
        kernel32.CloseHandle(h)
    return pid.value, name


def _client_rect(hwnd):
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def list_windows():
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        if user32.GetWindow(hwnd, 4):  # GW_OWNER: skip owned popups
            return True
        title = _title(hwnd)
        if not title or title in IGNORE_TITLES:
            return True
        x, y, w, h = _client_rect(hwnd)
        if w < 240 or h < 240:
            return True
        pid, proc = _proc_name(hwnd)
        text = (title + " " + proc).lower()
        score = max([s for k, s in GAME_HINTS if k in text] or [0])
        out.append({"id": int(hwnd), "pid": pid, "owner": proc, "title": title,
                    "label": title + ("  (" + proc + ")" if proc and proc.lower() not in title.lower() else ""),
                    "score": score})
        return True

    user32.EnumWindows(cb, 0)
    out.sort(key=lambda x: -x["score"])
    return out


def guess_game_window():
    wins = [w for w in list_windows() if w["score"] > 0]
    return wins[0] if wins else None


def window_info(win_id):
    hwnd = int(win_id)
    if not user32.IsWindow(hwnd):
        return None
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE: the game was minimized - bring it back so the bot can see it
        time.sleep(0.6)
        if user32.IsIconic(hwnd):
            return None
    x, y, w, h = _client_rect(hwnd)
    if w <= 0 or h <= 0:
        return None
    pid, _ = _proc_name(hwnd)
    return {"id": hwnd, "pid": pid, "x": float(x), "y": float(y), "w": float(w), "h": float(h), "onscreen": True}


def activate(pid_or_hwnd, hwnd=None):
    """Brings the game window to the front (engine passes pid; we use the remembered window)."""
    h = hwnd or _last_hwnd.get("h")
    if not h:
        return
    try:
        if user32.IsIconic(h):
            user32.ShowWindow(h, 9)  # SW_RESTORE
        fg = user32.GetForegroundWindow()
        if fg == h:
            return
        # No AttachThreadInput: when our own window is in front, joining its input queue from this worker
        # thread froze the app's UI and Windows closed it ("not responding"). A tiny Alt tap is enough to let
        # SetForegroundWindow work.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(h)
        user32.BringWindowToTop(h)
    except Exception:
        pass


def _root_at(x, y):
    hit = user32.WindowFromPoint(wt.POINT(int(round(x)), int(round(y))))
    return user32.GetAncestor(hit, 2) if hit else 0  # GA_ROOT


def ensure_front(win):
    """Makes sure clicks at the window's centre really land on the game. Returns True if they will."""
    h = int(win["id"])
    _last_hwnd["h"] = h
    cx, cy = win["x"] + win["w"] / 2, win["y"] + win["h"] / 2
    for _ in range(3):
        if _root_at(cx, cy) == h:
            return True
        activate(None, h)
        time.sleep(0.25)
    return _root_at(cx, cy) == h


def covering_title(win):
    """Title of the window that is on top of the game's centre (for the status message)."""
    root = _root_at(win["x"] + win["w"] / 2, win["y"] + win["h"] / 2)
    return _title(root) if root else ""


_last_hwnd = {}


# ---------------------------------------------------------------- capture

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def _print_window(hwnd, w, h):
    """Client-area screenshot even if covered (PW_CLIENTONLY | PW_RENDERFULLCONTENT)."""
    hdc_win = user32.GetDC(hwnd)
    hdc = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    old = gdi32.SelectObject(hdc, bmp)
    ok = user32.PrintWindow(hwnd, hdc, 3)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = w, -h, 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.SelectObject(hdc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc)
    user32.ReleaseDC(hwnd, hdc_win)
    if not ok:
        return None
    return Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)


def _is_blank(img):
    small = img.resize((32, 32))
    ext = small.convert("L").getextrema()
    return ext[1] - ext[0] < 8


def _grab(win_id):
    info = window_info(win_id)
    if not info:
        return None
    hwnd = info["id"]
    _last_hwnd["h"] = hwnd
    w, h = int(info["w"]), int(info["h"])
    img = None
    try:
        img = _print_window(hwnd, w, h)
    except Exception:
        img = None
    if img is None or _is_blank(img):
        # GPU-rendered emulators can come out black: screenshot the visible area instead
        activate(None, hwnd)
        time.sleep(0.25)
        x, y = int(info["x"]), int(info["y"])
        img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    return img


def _jpeg(img, max_width):
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
    b = io.BytesIO()
    img.convert("RGB").save(b, "JPEG", quality=85)
    return b.getvalue()


def capture_jpeg(win_id, max_width=900):
    img = _grab(win_id)
    return _jpeg(img, max_width) if img else None


def capture_full_jpeg(win_id):
    img = _grab(win_id)
    return _jpeg(img, 1400) if img else None


# ---------------------------------------------------------------- input

MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004


def to_screen(win, nx, ny):
    nx = min(max(float(nx), 5), 995)
    ny = min(max(float(ny), 5), 995)
    return win["x"] + win["w"] * nx / 1000.0, win["y"] + win["h"] * ny / 1000.0


def _move(x, y):
    user32.SetCursorPos(int(round(x)), int(round(y)))


def click(x, y):
    x += random.uniform(-2, 2)
    y += random.uniform(-2, 2)
    _move(x, y)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(random.uniform(0.06, 0.12))
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def drag(x1, y1, x2, y2, steps=18):
    _move(x1, y1)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    for i in range(1, steps + 1):
        t = i / steps
        _move(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
        time.sleep(0.02)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def press_escape():
    """'Back' in emulators (Esc = Android back in BlueStacks)."""
    user32.keybd_event(0x1B, 0, 0, 0)
    time.sleep(0.04)
    user32.keybd_event(0x1B, 0, 2, 0)


def mouse_position():
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return float(pt.x), float(pt.y)


# ---------------------------------------------------------------- permissions (none needed on Windows)

def has_screen_recording():
    return True


def ask_screen_recording():
    pass


def has_accessibility():
    return True


def ask_accessibility():
    pass


def fix_install_location():
    return False  # Windows needs no move / permission fixing


def reset_and_request_permissions(need_accessibility=True, wipe_all=False):
    pass


def open_settings(pane):
    pass


def open_url(url):
    os.startfile(url)
