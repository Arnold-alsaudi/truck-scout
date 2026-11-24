"""Installs a newer release by itself: download the zip, replace the app after it quits, reopen it.

Windows: the app folder (next to the .exe) is replaced by a small hidden .cmd script.
Mac: the .app bundle is replaced by a small shell script (files downloaded this way carry no quarantine flag).
"""
import os
import platform
import subprocess
import sys
import tempfile
import zipfile

import net


def frozen():
    return getattr(sys, "frozen", False)


def pick_asset(assets, plat):
    """The right zip for this computer from a release's (name, url) list."""
    if plat == "win":
        want = ["windows"]
    else:
        want = ["apple-silicon"] if platform.machine().lower() in ("arm64", "aarch64") else ["intel"]
    for name, url in assets:
        low = name.lower()
        if low.endswith(".zip") and all(w in low for w in want):
            return url
    return None


def install(url, plat):
    """Downloads and stages the update, starts the replace-and-reopen script. The caller must quit the app."""
    data = net.download(url)
    work = tempfile.mkdtemp(prefix="sss-update-")
    zpath = os.path.join(work, "update.zip")
    with open(zpath, "wb") as f:
        f.write(data)
    new_dir = os.path.join(work, "new")
    os.makedirs(new_dir)
    pid = os.getpid()

    if plat == "win":
        with zipfile.ZipFile(zpath) as z:
            z.extractall(new_dir)
        src = new_dir
        inner = os.path.join(new_dir, "Saad Shard Sniper")
        if os.path.isdir(inner):
            src = inner
        app_dir = os.path.dirname(sys.executable)
        exe = os.path.join(app_dir, "Saad Shard Sniper.exe")
        script = os.path.join(work, "update.cmd")
        with open(script, "w", encoding="utf-8") as f:
            f.write("@echo off\n")
            f.write(":wait\n")
            f.write('tasklist /FI "PID eq %d" | find "%d" >nul && (timeout /t 1 /nobreak >nul & goto wait)\n' % (pid, pid))
            f.write('robocopy "%s" "%s" /MIR /R:3 /W:1 >nul\n' % (src, app_dir))
            f.write('start "" "%s"\n' % exe)
        subprocess.Popen(["cmd", "/c", script], creationflags=0x08000000 | 0x00000008)  # no window, detached
        return

    # Mac: …/Saad Shard Sniper.app/Contents/MacOS/Saad Shard Sniper -> bundle path
    bundle = os.path.abspath(os.path.join(os.path.dirname(sys.executable), "..", ".."))
    subprocess.run(["ditto", "-x", "-k", zpath, new_dir], check=True)
    new_app = os.path.join(new_dir, "Saad Shard Sniper.app")
    if not os.path.isdir(new_app):
        raise RuntimeError("ملف التحديث ناقص")
    script = os.path.join(work, "update.sh")
    with open(script, "w") as f:
        f.write("#!/bin/sh\n")
        f.write("while kill -0 %d 2>/dev/null; do sleep 1; done\n" % pid)
        f.write('rm -rf "%s"\n' % bundle)
        f.write('ditto "%s" "%s"\n' % (new_app, bundle))
        f.write('xattr -cr "%s" 2>/dev/null\n' % bundle)
        f.write('open "%s"\n' % bundle)
    os.chmod(script, 0o755)
    subprocess.Popen(["/bin/sh", script], start_new_session=True)
