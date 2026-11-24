"""The scouting loop: screenshot the game window -> Gemini reads it -> send S trucks -> click to the next truck."""
import json
import os
import random
import re
import threading
import time
from collections import deque

import osapi as macos
import net
import vision

CONFIG_DIR = (os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Saad Shard Sniper")
              if macos.PLATFORM == "win" else os.path.expanduser("~/Library/Application Support/Saad Shard Sniper"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

DEFAULTS = {
    "mode": "auto",                 # auto = bot plays | watch = you play, bot only alerts
    "ai_provider": "claude",          # claude | gemini
    "claude_key": "",
    "claude_model": "claude-haiku-5-5",
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
    "interval": 2,
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
        if int(self.get("interval") or 2) >= 6:  # old slow default -> faster default
            self["interval"] = 2
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
        self._pending = None                # S truck waiting for its map coordinates
        self._prev_thumb = None             # last screenshot (small) to see if clicks change the screen
        self._acted = False
        self._stuck = 0
        self._covered_by = ""

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
        if not self._key():
            return self._set("error", "حط مفتاح Claude (أو Gemini) في الإعدادات")
        no_alerts = not c["discord_webhook"] and not (c["tg_token"] and c["tg_chat"])
        watch = self.mode() == "watch"
        perm_note = ""
        if not macos.has_screen_recording():
            try:
                macos.ask_screen_recording()  # macOS shows its own permission prompt / settings page
            except Exception:
                pass
        if not macos.has_screen_recording() or (not watch and not macos.has_accessibility()):
            perm_note = " · لو الماك طلب إذن تسجيل الشاشة أو إمكانية الوصول، وافق"
        if not self.resolve_window():
            return self._set("error", "مش لاقي نافذة اللعبة. افتح Last Z واختارها من القائمة")
        self.running = True
        self._history.clear()
        self._last_mouse = None
        self._pending = None
        self._prev_thumb, self._acted, self._stuck = None, False, 0
        self._watch_prev, self._watch_last_ask = None, 0.0
        self._set("running", ("بيراقب: " if watch else "شغال على: ") + (self.window_label or "نافذة اللعبة")
                  + (" · ⚠ التنبيهات مقفولة: حط لينك Discord" if no_alerts else "") + perm_note)
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
                if self.mode() != "watch" and self._user_moved_mouse():
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

                if self.mode() == "watch":
                    self._watch_step(win, shot)
                    errors = 0
                    continue

                self._check_moving(shot)
                try:
                    self._yellow = vision.yellow_trucks(shot)
                except Exception:
                    self._yellow = []
                ans = self._ask(shot)
                self._log(ans)
                errors = 0

                self._handle_trucks(ans.get("trucks") or [], win)
                self._after_step(ans)
                screen = str(ans.get("screen", "?"))
                act = ans.get("action") or {}
                done = self._act(act, win)
                self._note_opened(act)
                self._acted = done.split(" ")[0] in ("tap", "swipe", "back")
                self._history.append(screen + " -> " + done)
                if done == "noinput":
                    self._set("error", "⚠ الماك مانع البوت يضغط: فعّل Saad Shard Sniper في إعدادات النظام ← الخصوصية والأمن ← إمكانية الوصول، وبعدين اقفل التطبيق وافتحه")
                elif done == "blocked":
                    self._set("error", "نافذة اللعبة متغطية بـ '%s'… خلي اللعبة ظاهرة ومحدش فوقها" % self._covered_by)
                elif self._stuck >= 3:
                    self._set("error", "⚠ الضغطات مش بتأثر في اللعبة (الشاشة ما اتغيرتش 3 مرات). اتأكد إن اللعبة في المقدمة وإن إذن إمكانية الوصول مفعّل")
                else:
                    self._set("running", screen)

                wait = 8 if act.get("type") == "wait" else int(self.cfg["interval"])
                end = time.time() + wait + random.uniform(0, 0.6)
                while self.running and time.time() < end:
                    time.sleep(0.25)
            except Exception as e:  # keep going on any error
                errors += 1
                self._set("error", "خطأ: " + net.short_error(str(e)))
                time.sleep(min(120, 5 * errors))

    # ------------------------------------------------------------ yellow trucks & diagnostics

    def _note_opened(self, act):
        """Remember which yellow trucks were tapped, so the bot moves on to the next one; forget after Refresh."""
        target = str(act.get("target", "")).lower()
        if "refresh" in target:
            self._opened = []
            return
        if act.get("type") != "tap":
            return
        x, y = float(act.get("x", -1)), float(act.get("y", -1))
        for yx, yy in getattr(self, "_yellow", []):
            if abs(yx - x) < 60 and abs(yy - y) < 60:
                self._opened = (getattr(self, "_opened", []) + [(yx, yy)])[-20:]

    def _yellow_hint(self):
        ys = getattr(self, "_yellow", [])
        if not ys:
            return "Yellow trucks detected by colour on this screenshot: none.\n"
        opened = getattr(self, "_opened", [])
        rows = []
        for i, (x, y) in enumerate(ys, 1):
            done = any(abs(x - ox) < 60 and abs(y - oy) < 60 for ox, oy in opened)
            rows.append("  %d) x=%d y=%d%s" % (i, x, y, "  (already opened)" if done else ""))
        return ("YELLOW (rank S) trucks found by exact colour detection - to open one, tap EXACTLY one of these points "
                "(x,y in 0-1000), never any other truck:\n" + "\n".join(rows) + "\n"
                "Open the ones not yet opened; when all are opened, tap Refresh.\n")

    def _log(self, ans, note=""):
        """Appends each step to log.txt in the settings folder (for diagnosing problems)."""
        try:
            path = os.path.join(CONFIG_DIR, "log.txt")
            if os.path.exists(path) and os.path.getsize(path) > 1_000_000:
                os.replace(path, path + ".old")
            with open(path, "a", encoding="utf-8") as f:
                f.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), note,
                                        json.dumps(ans, ensure_ascii=False) if ans is not None else ""))
        except Exception:
            pass

    # ------------------------------------------------------------ watch mode
    def mode(self):
        """auto = the bot plays to find trucks; watch = the player plays, the bot only watches and alerts."""
        return "watch" if str(self.cfg.get("mode") or "auto").lower() == "watch" else "auto"

    @staticmethod
    def _thumb(shot):
        try:
            from PIL import Image
            import io
            return Image.open(io.BytesIO(shot)).convert("L").resize((48, 80))
        except Exception:
            return None

    @staticmethod
    def _diff(a, b):
        from PIL import ImageChops, ImageStat
        return ImageStat.Stat(ImageChops.difference(a, b)).mean[0]

    def _watch_step(self, win, shot):
        """Watch mode: never clicks. Asks the AI only when the screen really changed, at most once per interval,
        so standing on one screen costs nothing."""
        thumb = self._thumb(shot)
        changed = self._watch_prev is None or thumb is None or self._diff(self._watch_prev, thumb) >= 4.0
        due = time.time() - self._watch_last_ask >= max(3, int(self.cfg["interval"]))
        if changed and due:
            self._watch_prev = thumb
            self._watch_last_ask = time.time()
            ans = self._ask(shot)
            trucks = ans.get("trucks") or []
            mc = _clean_coords(ans.get("map_coords"))
            for t in trucks:
                if mc and not _clean_coords(t.get("coords")):
                    t["coords"] = mc
            self._handle_trucks(trucks, win, watch=True)
            self._set("running", "بيراقب: " + str(ans.get("screen", "?")))
        end = time.time() + 2.5
        while self.running and time.time() < end:
            time.sleep(0.25)

    # ------------------------------------------------------------ AI provider
    def _provider(self):
        """claude or gemini: the chosen one if its key is set, else whichever key exists."""
        c = self.cfg
        p = str(c.get("ai_provider") or "claude").lower()
        if p == "claude" and not c.get("claude_key") and c.get("gemini_key"):
            return "gemini"
        if p == "gemini" and not c.get("gemini_key") and c.get("claude_key"):
            return "claude"
        return p

    def _key(self):
        return self.cfg.get("claude_key" if self._provider() == "claude" else "gemini_key") or ""

    def _ask(self, shot):
        c = self.cfg
        if self._provider() == "claude":
            model = str(c.get("claude_model") or net.CLAUDE_MODEL).strip()
            try:
                return net.ask_claude(c["claude_key"], model, self._prompt(), shot)
            except net.ModelGone:
                c["claude_model"] = net.CLAUDE_MODEL
                c.save()
                return net.ask_claude(c["claude_key"], net.CLAUDE_MODEL, self._prompt(), shot)
        try:
            return net.ask_gemini(c["gemini_key"], self._model(), self._prompt(), shot)
        except net.ModelGone:
            c["gemini_model"] = "auto"
            c["gemini_model_resolved"] = ""
            c.save()
            return net.ask_gemini(c["gemini_key"], self._model(), self._prompt(), shot)

    def _model(self):
        m = str(self.cfg.get("gemini_model") or "auto").strip()
        if m and m.lower() != "auto":
            return m
        if not self.cfg.get("gemini_model_resolved"):
            self.cfg["gemini_model_resolved"] = net.pick_flash_model(self.cfg["gemini_key"])
            self.cfg.save()
        return self.cfg["gemini_model_resolved"]

    def _check_moving(self, shot):
        """Counts how many acted steps in a row left the screen unchanged (clicks not reaching the game)."""
        try:
            from PIL import Image, ImageChops, ImageStat
            import io
            thumb = Image.open(io.BytesIO(shot)).convert("L").resize((48, 80))
        except Exception:
            return
        prev, self._prev_thumb = self._prev_thumb, thumb
        if prev is None or not self._acted:
            return
        diff = ImageStat.Stat(ImageChops.difference(prev, thumb)).mean[0]
        self._stuck = self._stuck + 1 if diff < 1.5 else 0

    def _landed(self, x, y):
        """Did our mouse move really happen? If macOS blocks it (no Accessibility permission) the cursor stays
        put: then ask for the permission instead of thinking the user moved the mouse."""
        time.sleep(0.05)
        mx, my = macos.mouse_position()
        self._last_mouse = (mx, my)  # compare future movement with where the cursor really is
        if abs(mx - x) <= 12 and abs(my - y) <= 12:
            self._noinput = 0
            return True
        self._noinput = getattr(self, "_noinput", 0) + 1
        if self._noinput == 1:
            try:
                macos.ask_accessibility()
            except Exception:
                pass
        return False

    def _act(self, a, win):
        t = a.get("type", "wait")
        if t in ("tap", "swipe", "back"):
            if not macos.ensure_front(win):
                self._covered_by = macos.covering_title(win) or "نافذة تانية"
                return "blocked"
            time.sleep(0.15)
        if t == "tap":
            x, y = macos.to_screen(win, a.get("x", 500), a.get("y", 500))
            macos.click(x, y)
            if not self._landed(x, y):
                return "noinput"
            return "tap " + str(a.get("target", ""))
        if t == "swipe":
            x1, y1 = macos.to_screen(win, a.get("x", 500), a.get("y", 500))
            x2, y2 = macos.to_screen(win, a.get("x2", 500), a.get("y2", 500))
            macos.drag(x1, y1, x2, y2)
            if not self._landed(x2, y2):
                return "noinput"
            return "swipe " + str(a.get("target", ""))
        if t == "back":
            macos.press_escape()
            return "back"
        return "wait"

    # ------------------------------------------------------------ trucks & coordinates

    PENDING_MAX_STEPS = 8   # how many steps to spend getting a truck's coordinates before sending without them

    def _handle_trucks(self, trucks, win, watch=False):
        """Queues a wanted S truck; it is sent once its map coordinates are known (or after a few tries)."""
        c = self.cfg
        for t in trucks:
            try:
                rank = str(t.get("rank", "")).strip().upper()
                server = int(t.get("server") or 0)
                frags = int(t.get("s_fragments") or 0)
            except (TypeError, ValueError):
                continue
            player = str(t.get("player", "")).strip()
            if rank != "S":
                continue
            why = ""
            if frags < int(c["min_fragments"]):
                why = "مفيهاش شظايا S كفاية (%d)" % frags
            elif not server:
                why = "رقم السيرفر مش ظاهر"
            elif not c.server_allowed(server):
                why = "سيرفر #%d برا النطاق %s-%s" % (server, c["server_from"], c["server_to"])
            if why:
                self._set(status="شاحنة S (%s) ما اتبعتتش: %s" % (player or "?", why))
                self._log(t, "SKIPPED: " + why)
                continue
            key = "%d:%s" % (server, player.lower())
            if self._pending and self._pending["key"] == key:
                continue
            if key in (c.get("seen") or {}):
                continue
            truck = {"key": key, "server": server, "player": player, "frags": frags,
                     "arrival": str(t.get("arrival") or ""), "rewards": str(t.get("rewards") or ""),
                     "photo": macos.capture_full_jpeg(win["id"]), "steps": 0}
            coords = _clean_coords(t.get("coords"))
            if coords or watch:
                self._send(truck, coords)  # watch mode: no clicking to fetch coordinates
            elif self._pending is None:
                self._pending = truck
                self._set(status="لقيت شاحنة S على #%d… بجيب الإحداثيات" % server)

    def _after_step(self, ans):
        """While a truck is pending: take coordinates from the map, or give up after a few steps."""
        p = self._pending
        if not p:
            return
        p["steps"] += 1
        coords = _clean_coords(ans.get("map_coords"))
        if coords:
            self._send(p, coords)
        elif p["steps"] >= self.PENDING_MAX_STEPS:
            self._send(p, "")

    def _send(self, truck, coords):
        c = self.cfg
        if self._pending is truck:
            self._pending = None
        if c.already_sent(truck["key"]):
            return
        lines = ["🎯 **SHARD SNIPER** — شاحنة S!", "🌐 السيرفر: #%d" % truck["server"]]
        if truck["player"]:
            lines.append("👤 اللاعب: " + truck["player"])
        lines.append("📍 الإحداثيات: " + (coords if coords else "مش ظاهرة"))
        if truck["arrival"]:
            lines.append("⏱ وقت الوصول: " + truck["arrival"])
        lines.append("🧩 شظايا S: %d" % truck["frags"])
        if truck["rewards"]:
            lines.append("🎁 الجوايز: " + truck["rewards"])
        text = "\n".join(lines)
        sent = False
        if c["discord_webhook"]:
            try:
                net.discord(c["discord_webhook"], text, truck["photo"])
                sent = True
            except Exception as e:
                self._set(status="فشل الإرسال لـ Discord: " + net.short_error(str(e)))
        if c["tg_token"] and c["tg_chat"]:
            try:
                net.telegram(c["tg_token"], c["tg_chat"], text, truck["photo"])
                sent = True
            except Exception:
                pass
        if sent:
            c["found_count"] = int(c["found_count"]) + 1
            c.save()

    def _prompt(self):
        c = self.cfg
        watch = self.mode() == "watch"
        hist = "".join("- %s\n" % h for h in self._history) or "- none\n"
        p = self._pending
        knowledge = (
            "You look at ONE screenshot of the mobile game \"Last Z: Survival Shooter\" (shown in a desktop window: "
            "BlueStacks, the iPhone/iPad app on Mac, or another emulator; ignore emulator toolbars/side panels). "
            "The game UI may be in Arabic or English.\n\n"
            "== GAME KNOWLEDGE ==\n"
            "* Trade trucks (\"Intercity Trade\" / \"Mysterious Trade\" event): trucks drive between states (servers). "
            "The trade screen has two bottom tabs: \"Others' Trucks\" (OTHER players' trucks) and \"My Truck\" (the "
            "player's OWN truck).\n"
            "* Others' Trucks screen: a road with many trucks; trucks with an avatar picture and a name tag like \"[91TR] name\" "
            "are the ones you can open (tap the avatar). Top: \"Plunders Today: 0/4\", the checkbox \"Hide trucks of this State\", "
            "a \"Refresh\" dice and \"Records\" on the right. Truck COLOUR shows the rank: YELLOW/GOLDEN = S.\n"
            "* A truck CARD (bottom of the screen) shows: a big golden RANK letter (S is the best, then A, B, ...); the owner as "
            "\"Lv.25 Name\" with a power number; the server like \"#398\"; \"Arrival Time 04:23:10\"; a grid of reward "
            "icons with counts; buttons \"Plunder!\" (attack - NEVER) and \"Share\". On the card's left and right edges there "
            "are arrows \u2039 \u203a that cycle through ALL trucks (any rank) - do not use them.\n"
            "* S hero fragment = golden puzzle-piece icon with a letter S (\"Universal Orange Hero Fragment\"). Its count "
            "is the small number on the icon; s_fragments = total of those counts on the card (3 icons x1 = 3).\n"
            "* OWN truck (never report it): \"My Truck\" tab selected, or the card shows \"Trade Escorts\", "
            "\"Deploy Escorts\", \"Please deploy escorts\", \"Today's Trade Count\" or a refresh button.\n"
            "* COORDINATES: tapping \"Share\" on the card opens a small \"Share\" popup that shows the truck's location as "
            "\"[91TR]name [#:393 X:905 Y:925]\". Reading it is safe; close it with its X (top right) and NEVER press "
            "anything else inside it (no channel, no send, no copy).\n"
            "* Other screens you will meet: base/city view, battles, \"Hold & Drag\" runner mini-games, tutorials and "
            "story dialogs, VICTORY/DEFEAT result popups, offers/ads popups, chat, alliance screens.\n\n"
            "== WHAT TO REPORT ==\n"
            "In \"trucks\" list ONLY other players' trucks whose card or label is clearly readable now: give rank, "
            "server number, player name, s_fragments, arrival, coords (only if shown) and a short rewards list. "
            "Never invent values; if the server number is not visible use 0. Never list the player's own truck.\n"
            "Wanted: rank S with S fragments, servers #%s to #%s, except %s.\n\n"
            % (c["server_from"], c["server_to"], c["excluded"])
        )
        if watch:
            task = (
                "== YOUR JOB: WATCH ONLY ==\n"
                "The player is playing the game themselves right now. You must NOT act: always answer "
                "\"action\": {\"type\": \"wait\"}. Just read the screen and report wanted trucks and any map "
                "coordinates (\"map_coords\") that are visible.\n\n"
            )
        else:
            pending = ""
            if p:
                pending = (
                    ">>> CURRENT TASK (more important than scouting): you just found a wanted S truck (server #%d, "
                    "player \"%s\"). Get its COORDINATES: tap \"Share\" on its card (NOT Plunder!). In the Share popup read "
                    "\"#:393 X:905 Y:925\" and put it in \"map_coords\"; in the NEXT step close the popup with its X. "
                    "If the card is closed, reopen that truck first.\n" % (p["server"], p["player"])
                )
            task = (
                "== YOUR JOB: PLAY TO SCOUT TRUCKS ==\n"
                "Navigate to other players' trucks and inspect them one by one:\n"
                "1. From the base/city view click the round truck icon on the left side.\n"
                "2. Open the \"Others' Trucks\" tab; if a checkbox like \"Show trucks from other states only\" / "
                "\"hide trucks from this state\" exists, make sure it is ON.\n"
                "3. ONLY YELLOW / GOLDEN trucks are rank S. Tap ONLY a yellow truck that has an avatar and a name tag like "
                "\"[OWCH] Name\" to open its card. Ignore white, blue, green, purple and other colours completely - never open them. "
                "Read the card, then tap the NEXT yellow point from the list below. NEVER use the \u2039 \u203a arrows on the card "
                "(they cycle through ALL trucks, including non-S ones). Never tap empty road.\n"
                "4. When every yellow truck on screen has been checked (or there is none), tap the \"Refresh\" dice on the right "
                "side to get a new list, then continue. If Refresh asks to pay diamonds/gems, close that popup instead.\n"
                "5. Anything else in the way (tutorial, dialog, VICTORY/DEFEAT, offer popup): close it or press "
                "Continue/Skip/X to get back to the base view. In mini-games or battles, press back/X to leave.\n"
                + pending +
                "\nSTRICT RULES - never click: Loot, Plunder, Plunder!, Attack, Deploy, Deploy Escorts, Retry, Buy, "
                "Purchase, Recharge, anything costing diamonds/gold/gems, the OWN truck's refresh button, or chat. "
                "\"Share\" is allowed ONLY to open the popup and read coordinates (then close it with X). If unsure choose \"back\" or \"wait\". \"back\" presses Escape (works in "
                "emulators); in the iPhone app prefer the on-screen X / back arrow. Do not repeat an action that did "
                "not change the screen; try something else.\n\n"
                + self._yellow_hint() +
                "Recent steps:\n" + hist + "\n"
            )
        return knowledge + task + (
            "Answer ONLY JSON:\n"
            "{\"screen\": \"short description of the current screen\",\n"
            " \"trucks\": [ {\"server\": 398, \"player\": \"name or empty\", \"rank\": \"S|A|B|...\", \"s_fragments\": 3, "
            "\"arrival\": \"04:23:10\", \"coords\": \"X:123 Y:456 if shown on the card, else empty\", "
            "\"rewards\": \"short list\"} ],\n"
            " \"map_coords\": \"X:123 Y:456 if a world map with coordinates is shown, else empty\",\n"
            " \"action\": {\"type\": \"tap|swipe|back|wait\", \"x\": 0-1000, \"y\": 0-1000, \"x2\": 0-1000, \"y2\": 0-1000, "
            "\"target\": \"what you click\"}}\n"
            "x,y are 0-1000 of the screenshot width/height."
        )

def _clean_coords(v):
    """Keeps only something that looks like real coordinates (two numbers), formatted 'X:123 Y:456'."""
    if not v:
        return ""
    nums = re.findall(r"\d+", str(v))
    s = str(v)
    m = re.search(r"[Xx]\s*[:：]?\s*(\d+)\D+[Yy]\s*[:：]?\s*(\d+)", s)
    if m:
        return "X:%s Y:%s" % (m.group(1), m.group(2))
    if len(nums) >= 2:
        return "X:%s Y:%s" % (nums[-2], nums[-1])
    return ""