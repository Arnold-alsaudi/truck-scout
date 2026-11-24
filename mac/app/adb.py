"""Background collection: drive BlueStacks through its built-in ADB, with no mouse and no window focus.

Works while BlueStacks is minimized or covered: screenshots come from Android itself (screencap) and taps are
Android input events. BlueStacks must have "Android Debug Bridge (ADB)" enabled in Settings > Advanced.
"""
import io
import os
import re
import subprocess
import sys
import time

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW: no console flashes

ADB_CANDIDATES = [
    r"C:\Program Files\BlueStacks_nxt\HD-Adb.exe",
    r"C:\Program Files\BlueStacks\HD-Adb.exe",
    r"C:\Program Files (x86)\BlueStacks_nxt\HD-Adb.exe",
    "/Applications/BlueStacks.app/Contents/MacOS/hd-adb",
    "/Applications/BlueStacks Air.app/Contents/MacOS/hd-adb",
    "/opt/homebrew/bin/adb", "/usr/local/bin/adb",
]
CONF_CANDIDATES = [
    r"C:\ProgramData\BlueStacks_nxt\bluestacks.conf",
    r"C:\ProgramData\BlueStacks\bluestacks.conf",
    os.path.expanduser("~/Library/Application Support/BlueStacks/bluestacks.conf"),
]
DEFAULT_PORTS = [5555, 5565, 5575, 5585, 5595, 5605, 5615, 5625]


class AdbError(Exception):
    pass


def adb_path():
    for p in ADB_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _run(args, timeout=20, binary=False):
    exe = adb_path()
    if not exe:
        raise AdbError("مش لاقي ADB بتاع BlueStacks. اتأكد إن BlueStacks متثبت")
    r = subprocess.run([exe] + args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    if binary:
        return r.returncode, r.stdout
    return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace").strip()


def instance_ports():
    """[(instance name, port)] read from bluestacks.conf, plus whether ADB access is enabled."""
    found, enabled = [], None
    for conf in CONF_CANDIDATES:
        if not os.path.exists(conf):
            continue
        try:
            text = open(conf, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = re.search(r'bst\.enable_adb_access="(\d)"', text)
        if m:
            enabled = m.group(1) == "1"
        for name, port in re.findall(r'bst\.instance\.([^.]+)\.status\.adb_port="(\d+)"', text):
            found.append((name, int(port)))
        break
    return found, enabled


class Device:
    def __init__(self, serial):
        self.serial = serial
        self.w = self.h = 0

    def sh(self, *cmd, timeout=20):
        return _run(["-s", self.serial, "shell"] + list(cmd), timeout=timeout)

    def size(self):
        code, out = self.sh("wm", "size")
        m = re.findall(r"(\d+)x(\d+)", out)
        if not m:
            raise AdbError("مش قادر أعرف مقاس شاشة BlueStacks")
        self.w, self.h = int(m[-1][0]), int(m[-1][1])  # "Override size" wins if present
        return self.w, self.h

    def screenshot_jpeg(self, max_width=900):
        code, png = _run(["-s", self.serial, "exec-out", "screencap", "-p"], timeout=25, binary=True)
        if code != 0 or not png or not png.startswith(b"\x89PNG"):
            raise AdbError("BlueStacks ما رجعش صورة (اتأكد إنه شغال)")
        from PIL import Image
        img = Image.open(io.BytesIO(png)).convert("RGB")
        self.w, self.h = img.size
        if img.width > max_width:
            img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
        b = io.BytesIO()
        img.save(b, "JPEG", quality=85)
        return b.getvalue()

    def _px(self, nx, ny):
        nx = min(max(float(nx), 5), 995)
        ny = min(max(float(ny), 5), 995)
        return int(self.w * nx / 1000), int(self.h * ny / 1000)

    def tap(self, nx, ny):
        x, y = self._px(nx, ny)
        self.sh("input", "tap", str(x), str(y))

    def swipe(self, nx1, ny1, nx2, ny2):
        x1, y1 = self._px(nx1, ny1)
        x2, y2 = self._px(nx2, ny2)
        self.sh("input", "swipe", str(x1), str(y1), str(x2), str(y2), "450")

    def back(self):
        self.sh("input", "keyevent", "4")


def connect(preferred_port=0):
    """Finds a running BlueStacks instance with ADB and returns a Device, or raises AdbError with a clear message."""
    if not adb_path():
        raise AdbError("مش لاقي ADB بتاع BlueStacks. اتأكد إن BlueStacks متثبت")
    named, enabled = instance_ports()
    if enabled is False:
        raise AdbError("فعّل ADB في BlueStacks: الإعدادات ← Advanced ← Android Debug Bridge")
    ports = ([int(preferred_port)] if preferred_port else []) + [p for _, p in named] + DEFAULT_PORTS
    seen = set()
    for port in ports:
        if port in seen:
            continue
        seen.add(port)
        serial = "127.0.0.1:%d" % port
        try:
            _run(["connect", serial], timeout=6)
            code, out = _run(["-s", serial, "get-state"], timeout=6)
        except (subprocess.TimeoutExpired, AdbError):
            continue
        if code == 0 and "device" in out:
            d = Device(serial)
            d.size()
            return d
    raise AdbError("مش لاقي BlueStacks شغال بـ ADB. افتح BlueStacks واتأكد إن ADB مفعّل من الإعدادات ← Advanced")
