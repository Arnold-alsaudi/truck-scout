"""Saad Shard Sniper for Mac: a small always-handy window that runs the truck scout on the Last Z window."""
import hashlib
import json
import os
import sys
import threading
import time

import webview

import engine
import osapi as macos
import self_update
import net

VERSION = 1  # bumped by the release workflow tag mac-v<N>
CLIENT_ID = "owner-win" if macos.PLATFORM == "win" else "client1"  # entry in license.json (license.ps1)
LICENSE_EVERY = 10 * 60    # seconds between license checks
OFFLINE_GRACE = 48 * 3600  # how long the app keeps working without reaching the license file


def resource(path):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, path)


class Api:
    """Functions the HTML page calls through window.pywebview.api."""

    def __init__(self):
        self._window = None
        self._js_lock = threading.Lock()
        self._js_pending = {}
        self._js_busy = False
        self._engine = engine.Engine(self._push)
        self._lic = {"status": "active", "message": ""}
        threading.Thread(target=self._license_loop, daemon=True).start()

    # ---- license (owner can lock with a password or disable this copy remotely)
    def _license_now(self):
        c = self._engine.cfg
        now = time.time()
        remote = net.fetch_license(CLIENT_ID)
        if remote is not None:
            c["lic"] = remote
            c["lic_checked"] = now
            c.save()
        elif now - float(c.get("lic_checked") or 0) > OFFLINE_GRACE:
            return {"status": "offline", "message": "لازم تتصل بالإنترنت عشان التطبيق يتحقق من الترخيص."}
        lic = c.get("lic") or {}
        status = str(lic.get("status") or "active").lower()
        msg = str(lic.get("message") or "")
        if status == "disabled":
            return {"status": "disabled", "message": msg or "النسخة دي موقوفة. تواصل مع المطور."}
        if status == "locked":
            want = str(lic.get("pass_sha256") or "").lower()
            if want and c.get("unlock_hash") == want:
                return {"status": "active", "message": ""}
            return {"status": "locked", "message": msg or "التطبيق مقفول. اكتب الباسورد اللي استلمته من المطور."}
        return {"status": "active", "message": ""}

    def _license_loop(self):
        while True:
            try:
                self._apply_license(self._license_now())
            except Exception:
                pass
            time.sleep(LICENSE_EVERY)

    def _apply_license(self, lic):
        self._lic = lic
        if lic["status"] != "active" and self._engine.running:
            self._engine.stop()
        self._js("window.onLicense && onLicense(%s)" % json.dumps(lic, ensure_ascii=False))

    def _js(self, code):
        """Sends an update to the page without waiting for it. evaluate_js waits for the UI thread, and when
        that thread is itself waiting on an API call (or busy), the whole window freezes and Windows closes it."""
        if not self._window:
            return
        key = code.split("(", 1)[0]  # one pending update per kind; newer replaces older
        with self._js_lock:
            self._js_pending[key] = code
            if self._js_busy:
                return
            self._js_busy = True
        threading.Thread(target=self._js_flush, daemon=True).start()

    def _js_flush(self):
        while True:
            with self._js_lock:
                if not self._js_pending:
                    self._js_busy = False
                    return
                _, code = self._js_pending.popitem()
            try:
                self._window.evaluate_js(code)
            except Exception:
                pass

    def license(self):
        """Checks right now (called when the window opens) and returns the result."""
        try:
            self._lic = self._license_now()
        except Exception:
            pass
        if self._lic["status"] != "active" and self._engine.running:
            self._engine.stop()
        return self._lic

    def unlock(self, password):
        lic = self._engine.cfg.get("lic") or {}
        want = str(lic.get("pass_sha256") or "").lower()
        got = hashlib.sha256(str(password).strip().encode("utf-8")).hexdigest()
        if want and got == want:
            self._engine.cfg["unlock_hash"] = got
            self._engine.cfg.save()
            self._apply_license({"status": "active", "message": ""})
            return True
        return False

    def _push(self, state):
        self._js("window.onState && onState(%s)" % json.dumps(state, ensure_ascii=False))

    # ---- state & settings
    def get_all(self):
        c = self._engine.cfg
        return {
            "version": VERSION,
            "platform": macos.PLATFORM,
            "state": self._engine.snapshot(),
            "config": {k: c[k] for k in ("mode", "ai_provider", "claude_key", "gemini_key", "gemini_model", "discord_webhook", "tg_token", "tg_chat",
                                         "server_from", "server_to", "excluded", "min_fragments", "interval",
                                         "window_id")},
        }

    def save_config(self, values):
        c = self._engine.cfg
        for k in ("mode", "ai_provider", "claude_key", "gemini_key", "gemini_model", "discord_webhook", "tg_token", "tg_chat", "excluded"):
            if k in values:
                c[k] = str(values[k]).strip()
        for k in ("server_from", "server_to", "min_fragments", "interval", "window_id"):
            if k in values:
                try:
                    c[k] = int(values[k])
                except (TypeError, ValueError):
                    pass
        if not c["gemini_model"]:
            c["gemini_model"] = "auto"
        c["interval"] = max(1, int(c["interval"]))
        c.save()
        return True

    def reset_seen(self):
        c = self._engine.cfg
        c["seen"] = {}
        c["found_count"] = 0
        c.save()
        self._push(self._engine.snapshot())

    # ---- windows
    def list_windows(self):
        return [{"id": w["id"], "label": w["label"], "score": w["score"]} for w in macos.list_windows()]

    # ---- run
    def start(self):
        if self._lic.get("status") != "active":
            self._apply_license(self._lic)
            return self._engine.snapshot()
        self._engine.start()
        return self._engine.snapshot()

    def stop(self):
        self._engine.stop()
        return self._engine.snapshot()

    # ---- permissions
    def permissions(self):
        return {"screen": macos.has_screen_recording(), "ax": macos.has_accessibility()}

    def ask_screen(self):
        macos.ask_screen_recording()

    def ask_ax(self):
        macos.ask_accessibility()

    # ---- tests & updates
    def test_ai(self):
        """One tiny request to the chosen AI to prove the key works."""
        e = self._engine
        if not e._key():
            return "حط المفتاح الأول"
        try:
            if e._provider() == "claude":
                import anthropic
                client = anthropic.Anthropic(api_key=e.cfg["claude_key"], timeout=30.0)
                r = client.messages.create(model=e.cfg.get("claude_model") or net.CLAUDE_MODEL, max_tokens=256,
                                           output_config={"effort": "low"},
                                           messages=[{"role": "user", "content": "Reply with the word OK."}])
                return "ok:Claude (%s)" % r.model
            net.pick_flash_model(e.cfg["gemini_key"])
            return "ok:Gemini"
        except Exception as ex:
            return net.short_error(str(getattr(ex, "message", ex)))

    def test_discord(self):
        url = self._engine.cfg["discord_webhook"]
        if not url:
            return "حط لينك Discord الأول"
        try:
            net.discord(url, "✅ Saad Shard Sniper (%s) متوصل وشغال" % ("Windows" if macos.PLATFORM == "win" else "Mac"))
            return "ok"
        except Exception as e:
            return net.short_error(str(e))

    def check_update(self):
        """A newer release (forced update), or None. Stops the bot while an update is pending."""
        try:
            r = net.newer_release(VERSION, macos.PLATFORM + "-v")
        except Exception:
            r = None
        if not r:
            return None
        self._update = r
        if self._engine.running:
            self._engine.stop()
        asset = self_update.pick_asset(r[2], macos.PLATFORM)
        return {"version": r[0], "url": r[1], "auto": bool(asset and self_update.frozen())}

    def do_update(self):
        """Downloads and installs the pending update, then quits so the script can swap the app and reopen it."""
        r = getattr(self, "_update", None)
        if not r:
            return "مفيش تحديث"
        url = self_update.pick_asset(r[2], macos.PLATFORM)
        if not url or not self_update.frozen():
            macos.open_url(r[1])
            return "open"
        try:
            self_update.install(url, macos.PLATFORM)
        except Exception as e:
            return "فشل التحديث: " + net.short_error(str(e))
        threading.Timer(0.8, lambda: os._exit(0)).start()
        return "ok"

    def updated_note(self):
        """Version number if this is the first start after an update (to show 'updated'), else 0."""
        c = self._engine.cfg
        last = int(c.get("last_version") or 0)
        if last != VERSION:
            c["last_version"] = VERSION
            c.save()
        return VERSION if 0 < last < VERSION else 0

    def open_url(self, url):
        macos.open_url(url)


def main():
    if "--selftest" in sys.argv:
        # used by the build: proves the bundle imports and the macOS APIs answer
        print("ui:", os.path.exists(resource(os.path.join("ui", "index.html"))))
        print("windows:", len(macos.list_windows()))
        print("screen:", macos.has_screen_recording(), "ax:", macos.has_accessibility())
        print("license:", net.fetch_license(CLIENT_ID))
        print("selftest ok")
        return
    if macos.PLATFORM == "mac" and self_update.frozen():
        # running from Downloads / a translocated copy never keeps permissions: move to Applications and reopen
        if macos.fix_install_location():
            sys.exit(0)
    api = Api()
    if macos.PLATFORM == "mac":
        # clear stale permission entries from older versions and show macOS's prompts right away
        need_ax = api._engine.mode() != "watch"
        threading.Timer(2.0, lambda: macos.reset_and_request_permissions(need_ax)).start()
    win = webview.create_window(
        "Saad Shard Sniper", resource(os.path.join("ui", "index.html")), js_api=api,
        width=420, height=760, min_size=(380, 560), background_color="#0c0516", text_select=False)
    api._window = win
    webview.start(debug=False)


if __name__ == "__main__":
    main()
