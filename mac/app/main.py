"""Saad Shard Sniper for Mac: a small always-handy window that runs the truck scout on the Last Z window."""
import json
import os
import sys
import threading

import webview

import engine
import macos
import net

VERSION = 1  # bumped by the release workflow tag mac-v<N>


def resource(path):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, path)


class Api:
    """Functions the HTML page calls through window.pywebview.api."""

    def __init__(self):
        self.window = None
        self.engine = engine.Engine(self._push)

    def _push(self, state):
        if self.window:
            self.window.evaluate_js("window.onState && onState(%s)" % json.dumps(state, ensure_ascii=False))

    # ---- state & settings
    def get_all(self):
        c = self.engine.cfg
        return {
            "version": VERSION,
            "state": self.engine.snapshot(),
            "config": {k: c[k] for k in ("gemini_key", "gemini_model", "discord_webhook", "tg_token", "tg_chat",
                                         "server_from", "server_to", "excluded", "min_fragments", "interval",
                                         "window_id")},
        }

    def save_config(self, values):
        c = self.engine.cfg
        for k in ("gemini_key", "gemini_model", "discord_webhook", "tg_token", "tg_chat", "excluded"):
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
        c["interval"] = max(3, int(c["interval"]))
        c.save()
        return True

    def reset_seen(self):
        c = self.engine.cfg
        c["seen"] = {}
        c["found_count"] = 0
        c.save()
        self._push(self.engine.snapshot())

    # ---- windows
    def list_windows(self):
        return [{"id": w["id"], "label": w["label"], "score": w["score"]} for w in macos.list_windows()]

    # ---- run
    def start(self):
        self.engine.start()
        return self.engine.snapshot()

    def stop(self):
        self.engine.stop()
        return self.engine.snapshot()

    # ---- permissions
    def permissions(self):
        return {"screen": macos.has_screen_recording(), "ax": macos.has_accessibility()}

    def ask_screen(self):
        macos.ask_screen_recording()

    def ask_ax(self):
        macos.ask_accessibility()

    # ---- tests & updates
    def test_discord(self):
        url = self.engine.cfg["discord_webhook"]
        if not url:
            return "حط لينك Discord الأول"
        try:
            net.discord(url, "✅ Saad Shard Sniper (Mac) متوصل وشغال")
            return "ok"
        except Exception as e:
            return net.short_error(str(e))

    def check_update(self):
        try:
            r = net.newer_mac_release(VERSION)
        except Exception:
            r = None
        return {"version": r[0], "url": r[1]} if r else None

    def open_url(self, url):
        macos.open_url(url)


def main():
    if "--selftest" in sys.argv:
        # used by the build: proves the bundle imports and the macOS APIs answer
        print("ui:", os.path.exists(resource(os.path.join("ui", "index.html"))))
        print("windows:", len(macos.list_windows()))
        print("screen:", macos.has_screen_recording(), "ax:", macos.has_accessibility())
        print("selftest ok")
        return
    api = Api()
    win = webview.create_window(
        "Saad Shard Sniper", resource(os.path.join("ui", "index.html")), js_api=api,
        width=420, height=760, min_size=(380, 560), background_color="#0c0516", text_select=False)
    api.window = win
    webview.start(debug=False)


if __name__ == "__main__":
    main()
