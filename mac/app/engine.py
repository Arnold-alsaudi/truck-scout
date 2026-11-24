"""The scouting loop: screenshot the game window -> Gemini reads it -> send S trucks -> click to the next truck."""
import json
import os
import random
import re
import threading
import time
from collections import deque

import macos
import net

CONFIG_DIR = os.path.expanduser("~/Library/Application Support/Saad Shard Sniper")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "gemini_key": "",
    "gemini_model": "auto",
    "gemini_model_resolved": "",
    "discord_webhook": "",
    "tg_token": "",
    "tg_chat": "",
    "server_from": 385,
    "server_to": 416,
    "excluded": "405",
    "min_fragments": 1,
    "interval": 6,
    "window_id": 0,
    "found_count": 0,
    "seen": {},
}


class Config(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                self.update(json.load(f))
        except (OSError, ValueError):
            pass
        if self.get("gemini_model") == "gemini-2.5-flash":
            self["gemini_model"] = "auto"

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CONFIG_FILE)

    def server_allowed(self, server):
        if server < int(self["server_from"]) or server > int(self["server_to"]):
            return False
        excluded = {s.strip() for s in re.split(r"[,\s]+", str(self["excluded"])) if s.strip()}
        return str(server) not in excluded

    def already_sent(self, key):
        """Remember each truck for 12 hours so it is not sent twice."""
        now = time.time()
        seen = {k: t for k, t in self["seen"].items() if now - t < 12 * 3600}
        found = key in seen
        if not found:
            seen[key] = now
        self["seen"] = seen
        self.save()
        return found


class Engine:
    def __init__(self, on_change):
        self.cfg = Config()
        self.on_change = on_change          # called with a state dict whenever something changes
        self.running = False
        self.state = "stopped"              # stopped | running | paused | error
        self.status = "واقف"
        self.window = None                  # dict from macos.window_info
        self.window_label = ""
        self._thread = None
        self._history = deque(maxlen=8)
        self._last_mouse = None

    # ------------------------------------------------------------ state

    def snapshot(self):
        return {
            "running": self.running,
            "state": self.state,
            "status": self.status,
            "found": self.cfg["found_count"],
            "window": self.window_label,
            "screen_ok": macos.has_screen_recording(),
            "ax_ok": macos.has_accessibility(),
        }

    def _set(self, state=None, status=None):
        if state:
            self.state = state
        if status is not None:
            self.status = status
        try:
            self.on_change(self.snapshot())
        except Exception:
            pass

    # ------------------------------------------------------------ window

    def resolve_window(self):
        wid = int(self.cfg.get("window_id") or 0)
        info = macos.window_info(wid) if wid else None
        if info is None:
            g = macos.guess_game_window()
            if g:
                info = macos.window_info(g["id"])
                self.window_label = g["label"]
        else:
            match = [w for w in macos.list_windows() if w["id"] == wid]
            self.window_label = match[0]["label"] if match else self.window_label
        self.window = info
        return info

    # ------------------------------------------------------------ start / stop

    def start(self):
        if self.running:
            return
        c = self.cfg
        if not c["gemini_key"]:
            return self._set("error", "حط مفتاح Gemini في الإعدادات")
        if not c["discord_webhook"] and not (c["tg_token"] and c["tg_chat"]):
            return self._set("error", "حط لينك Discord في الإعدادات")
        if not macos.has_screen_recording() or not macos.has_accessibility():
            return self._set("error", "ناقص إذن تسجيل الشاشة أو إمكانية الوصول")
        if not self.resolve_window():
            return self._set("error", "مش لاقي نافذة اللعبة. افتح Last Z واختارها من القائمة")
        self.running = True
        self._history.clear()
        self._last_mouse = None
        self._set("running", "شغال على: " + (self.window_label or "نافذة اللعبة"))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False
        self._set("stopped", "واقف")

    # ------------------------------------------------------------ loop

    def _user_moved_mouse(self):
        """If the user grabs the mouse, pause so we don't fight them."""
        if self._last_mouse is None:
            return False
        x, y = macos.mouse_position()
        lx, ly = self._last_mouse
        return abs(x - lx) > 25 or abs(y - ly) > 25

    def _loop(self):
        errors = 0
        while self.running:
            try:
                if self._user_moved_mouse():
                    self._set("paused", "اتوقف مؤقتاً لأنك حركت الماوس… هيكمل بعد 20 ثانية")
                    pos = macos.mouse_position()
                    idle_since = time.time()
                    while self.running and time.time() - idle_since < 20:
                        time.sleep(1)
                        now = macos.mouse_position()
                        if abs(now[0] - pos[0]) > 3 or abs(now[1] - pos[1]) > 3:
                            pos, idle_since = now, time.time()
                    self._last_mouse = None
                    if not self.running:
                        break

                win = self.resolve_window()
                if not win:
                    self._set("error", "نافذة اللعبة اتقفلت… مستني ترجع")
                    time.sleep(5)
                    continue
                shot = macos.capture_jpeg(win["id"])
                if not shot:
                    self._set("error", "مش قادر أصوّر النافذة. اتأكد من إذن تسجيل الشاشة")
                    time.sleep(5)
                    continue

                try:
                    ans = net.ask_gemini(self.cfg["gemini_key"], self._model(), self._prompt(), shot)
                except net.ModelGone:
                    self.cfg["gemini_model"] = "auto"
                    self.cfg["gemini_model_resolved"] = ""
                    self.cfg.save()
                    ans = net.ask_gemini(self.cfg["gemini_key"], self._model(), self._prompt(), shot)
                errors = 0

                self._handle_trucks(ans.get("trucks") or [], win)
                screen = str(ans.get("screen", "?"))
                act = ans.get("action") or {}
                done = self._act(act, win)
                self._history.append(screen + " -> " + done)
                self._set("running", screen)

                wait = 30 if act.get("type") == "wait" else int(self.cfg["interval"])
                end = time.time() + wait + random.uniform(0, 2.5)
                while self.running and time.time() < end:
                    time.sleep(0.25)
            except Exception as e:  # keep going on any error
                errors += 1
                self._set("error", "خطأ: " + net.short_error(str(e)))
                time.sleep(min(120, 5 * errors))

    def _model(self):
        m = str(self.cfg.get("gemini_model") or "auto").strip()
        if m and m.lower() != "auto":
            return m
        if not self.cfg.get("gemini_model_resolved"):
            self.cfg["gemini_model_resolved"] = net.pick_flash_model(self.cfg["gemini_key"])
            self.cfg.save()
        return self.cfg["gemini_model_resolved"]

    def _act(self, a, win):
        t = a.get("type", "wait")
        if t in ("tap", "swipe", "back"):
            macos.activate(win["pid"])
            time.sleep(0.15)
        if t == "tap":
            x, y = macos.to_screen(win, a.get("x", 500), a.get("y", 500))
            macos.click(x, y)
            self._last_mouse = (x, y)
            return "tap " + str(a.get("target", ""))
        if t == "swipe":
            x1, y1 = macos.to_screen(win, a.get("x", 500), a.get("y", 500))
            x2, y2 = macos.to_screen(win, a.get("x2", 500), a.get("y2", 500))
            macos.drag(x1, y1, x2, y2)
            self._last_mouse = (x2, y2)
            return "swipe " + str(a.get("target", ""))
        if t == "back":
            macos.press_escape()
            return "back"
        return "wait"

    def _handle_trucks(self, trucks, win):
        c = self.cfg
        for t in trucks:
            try:
                rank = str(t.get("rank", "")).strip().upper()
                server = int(t.get("server") or 0)
                frags = int(t.get("s_fragments") or 0)
            except (TypeError, ValueError):
                continue
            player = str(t.get("player", "")).strip()
            if rank != "S" or frags < int(c["min_fragments"]) or not server or not c.server_allowed(server):
                continue
            if c.already_sent("%d:%s" % (server, player.lower())):
                continue
            lines = ["🎯 **SHARD SNIPER** — شاحنة S!", "🌐 السيرفر: #%d" % server]
            if player:
                lines.append("👤 اللاعب: " + player)
            if t.get("coords"):
                lines.append("📍 الإحداثيات: " + str(t["coords"]))
            if t.get("arrival"):
                lines.append("⏱ وقت الوصول: " + str(t["arrival"]))
            lines.append("🧩 شظايا S: %d" % frags)
            if t.get("rewards"):
                lines.append("🎁 الجوايز: " + str(t["rewards"]))
            text = "\n".join(lines)
            photo = macos.capture_full_jpeg(win["id"])
            sent = False
            if c["discord_webhook"]:
                try:
                    net.discord(c["discord_webhook"], text, photo)
                    sent = True
                except Exception as e:
                    self._set(status="فشل الإرسال لـ Discord: " + net.short_error(str(e)))
            if c["tg_token"] and c["tg_chat"]:
                try:
                    net.telegram(c["tg_token"], c["tg_chat"], text, photo)
                    sent = True
                except Exception:
                    pass
            if sent:
                c["found_count"] = int(c["found_count"]) + 1
                c.save()

    def _prompt(self):
        c = self.cfg
        hist = "".join("- %s\n" % h for h in self._history) or "- none\n"
        return (
            "You control the mobile game \"Last Z: Survival Shooter\" shown in a desktop window on a Mac "
            "(BlueStacks, the iPhone/iPad app, or another emulator; UI may be Arabic or English). You see one screenshot "
            "of that window. Ignore emulator toolbars/side panels around the game.\n\n"
            "GOAL: scout other players' trade trucks (Intercity Trade / Mysterious Trade event) and find trucks of rank S "
            "(a big golden letter S on the truck card) whose rewards include S hero fragments (golden puzzle-piece icons "
            "with a letter S, \"Universal Orange Hero Fragment\"). Wanted servers: #%s to #%s, excluding: %s.\n\n"
            "HOW TO NAVIGATE:\n"
            "1. In the base/city view, the trade truck button is a round icon with a truck on the left side. Click it.\n"
            "2. In the trade screen, open the tab \"Others' Trucks\" (bottom). If there is a checkbox like \"Show trucks "
            "from other states only\" / \"hide trucks from this state\", make sure it is ON.\n"
            "3. Trucks show a server number like #398. Click a truck from a wanted server to open its card and read rank, "
            "rewards, player name, arrival time and any coordinates (X:Y). Then close the card (X button, or back) and "
            "continue with the next truck. Swipe the list or the road to reveal more trucks; use a refresh of the others' "
            "truck list only if it is free.\n"
            "4. If a popup/ad/reward dialog is in the way, close it with its X button.\n\n"
            "STRICT RULES - never click: Loot, Plunder, Attack, Deploy, Deploy Escorts, Share, Buy, Purchase, Recharge, "
            "anything costing diamonds/gold/gems, the refresh button of the player's OWN truck, or chat. You only look. "
            "If unsure, choose \"back\" or \"wait\". \"back\" presses Escape (works in emulators); in the iPhone app prefer "
            "clicking the on-screen X / back arrow.\n\n"
            "Recent steps (avoid repeating loops):\n%s\n"
            "Answer ONLY JSON:\n"
            "{\"screen\": \"short description of the current screen\",\n"
            " \"trucks\": [ {\"server\": 398, \"player\": \"name or empty\", \"rank\": \"S|A|B|...\", \"s_fragments\": 3, "
            "\"arrival\": \"04:23:10\", \"coords\": \"X:123 Y:456 or empty\", \"rewards\": \"short list\"} ],\n"
            " \"action\": {\"type\": \"tap|swipe|back|wait\", \"x\": 0-1000, \"y\": 0-1000, \"x2\": 0-1000, \"y2\": 0-1000, "
            "\"target\": \"what you click\"}}\n"
            "x,y are normalized 0-1000 of the screenshot width/height. List in \"trucks\" ONLY trucks whose rank and "
            "rewards are clearly visible right now; otherwise []."
            % (c["server_from"], c["server_to"], c["excluded"], hist)
        )
