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
        self._opened, self._yellow = [], []
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

                step_t0 = time.time()
                win = self.resolve_window()
                if not win:
                    self._set("error", "نافذة اللعبة اتقفلت… مستني ترجع")
                    time.sleep(5)
                    continue
                big = macos.capture_jpeg(win["id"], max_width=1400)
                if not big:
                    self._set("error", "مش قادر أصوّر النافذة. اتأكد من إذن تسجيل الشاشة")
                    time.sleep(5)
                    continue
                try:
                    reading_card = "yellow truck" in getattr(self, "_last_code", "")
                    shot, self._card = vision.main_and_card(big, 640 if reading_card else 900)
                except Exception:
                    shot, self._card = big, None

                if self.mode() == "watch":
                    self._watch_step(win, shot)
                    errors = 0
                    continue

                self._shot = shot
                self._check_moving(shot)
                try:
                    self._yellow = vision.yellow_trucks(shot)
                except Exception:
                    self._yellow = []
                last = getattr(self, "_last_code", "")
                skip_ai = (not self._pending and not getattr(self, "_need_close", False)
                           and getattr(self, "_refresh_xy", None) is not None
                           and any(k in last for k in ("Refresh dice (code)", "close Share popup (code)",
                                                        "waiting for trucks after Refresh (code)", "Refresh cooldown (code)")))
                quick = None
                p = self._pending
                if not skip_ai and p and p.get("share_tries") == 1 and self._provider() == "claude":
                    # Share was pressed: read ONLY the popup (small crop, one-line question) - about a second,
                    # instead of the whole screen with the long instructions (5-8 s)
                    try:
                        q = net.read_share_popup(self.cfg["claude_key"],
                                                 str(self.cfg.get("claude_model") or net.CLAUDE_MODEL).strip(),
                                                 vision.popup_band(big))
                    except Exception:
                        q = None
                    if isinstance(q, dict):
                        if q.get("open"):
                            cxy = q.get("close_xy")
                            close = list(vision.band_to_screen(*cxy)) if isinstance(cxy, list) and len(cxy) == 2 else []
                            quick = {"screen": "Share popup (quick read)", "trucks": [],
                                     "map_coords": str(q.get("coords") or ""), "popup_close_xy": close,
                                     "action": {"type": "wait"}}
                        else:
                            quick = {"screen": "waiting for Share popup (quick read)", "trucks": [],
                                     "action": {"type": "none"}}
                if skip_ai:
                    ans = {"screen": "trucks road", "trucks": [], "action": {"type": "none"}}
                elif quick:
                    ans = quick
                    self._log(ans)
                else:
                    ans = self._ask(shot)
                    self._log(ans)
                errors = 0
                self._handle_trucks(ans.get("trucks") or [], win)
                self._after_step(ans)
                screen = str(ans.get("screen", "?"))
                rxy = ans.get("refresh_xy") or []
                if isinstance(rxy, list) and len(rxy) == 2:
                    self._refresh_xy = (float(rxy[0]), float(rxy[1]))
                act = self._drive_trucks(ans.get("action") or {}, ans)
                act = self._truck_icon(act, ans, screen)
                done = self._act(act, win)
                self._last_code = str(act.get("target", "")) if "(code)" in str(act.get("target", "")) else ""
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

                # Every step takes the seconds the user chose, counted from the step's start: the screenshot,
                # the AI's reading and the tap all happen inside it ("2" = one step every 2 seconds). Only the
                # game's own animations set a small minimum (a card needs ~0.4 s to open, new trucks ~1 s to
                # drive in after Refresh), and the AI's reply can't be cut short if it takes longer.
                tgt = str(act.get("type", "")) + " " + str(act.get("target", ""))
                floor = 1.0 if ("Refresh dice" in tgt or act.get("type") == "wait") else (
                    0.4 if "yellow truck" in tgt else 0.0)
                wait = max(floor, float(self.cfg["interval"]) - (time.time() - step_t0))
                end = time.time() + wait
                while self.running and time.time() < end:
                    time.sleep(0.1)
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

    def _drive_trucks(self, ai_action, ans):
        """On the trucks road the CODE decides what to tap; the AI only reads the screen.
        Order: close the Share popup after a send -> press Share for a found S truck -> open the next unopened
        yellow truck -> press Refresh once (with a cooldown so it never double-refreshes)."""
        now = time.time()
        xy = lambda k: ans.get(k) if isinstance(ans.get(k), list) and len(ans.get(k)) == 2 else None
        if xy("popup_close_xy"):
            self._last_close_xy = xy("popup_close_xy")  # remembered for next time

        scr = str(ans.get("screen", "")).lower()
        popup_open = bool(xy("popup_close_xy")) or ("share" in scr and "popup" in scr)
        if popup_open or getattr(self, "_need_close", False):
            # The Share popup is open. Its rows (World, Alliance, chats) SEND the truck to that channel when
            # tapped, so inside it the bot may only press the X - checked so it is never a blue send button.
            p = self._pending
            if p and not _clean_coords(ans.get("map_coords")) and p["steps"] < self.PENDING_MAX_STEPS:
                return {"type": "none", "target": "reading coordinates in Share popup (code)"}
            if p:
                self._send(p, "")                         # coordinates not readable: alert without them
            self._need_close = False
            cxy = xy("popup_close_xy") or getattr(self, "_last_close_xy", None)
            safe = False
            if cxy:
                try:
                    safe = (cxy[1] < 600 and not vision.is_blue_button(self._shot, cxy[0], cxy[1])
                            and not vision.is_popup_row(self._shot, cxy[0], cxy[1]))
                except Exception:
                    safe = False
            if safe:
                return {"type": "tap", "x": cxy[0], "y": cxy[1], "target": "close Share popup (code)"}
            return {"type": "back", "target": "close Share popup (code)"}

        p = self._pending
        if p:                                             # found S truck: press Share on its card ourselves
            if p.get("share_tries", 0) >= 1:
                # Share was pressed already. NEVER press it again: if the popup opened a moment late, a second
                # press would land on a channel row inside it and send the truck to that chat.
                if p["steps"] >= 4:
                    self._send(p, "")                     # popup never came: alert without coordinates
                    return {"type": "none", "target": "Share popup did not open (code)"}
                return {"type": "none", "target": "waiting for Share popup (code)"}
            # the button is found by its colour (blue, with the orange Plunder! beside it), never guessed:
            # the AI's own guess of its position was often off and opened reward tooltips instead
            try:
                sxy = vision.share_button(self._shot)
            except Exception:
                sxy = None
            if sxy:
                p["share_tries"] = 1
                return {"type": "tap", "x": sxy[0], "y": sxy[1], "target": "Share (code)"}
            if p["steps"] >= 3:
                self._send(p, "")                         # card gone / button not found: send without coords
            else:
                return {"type": "none", "target": "waiting for the card (code)"}

        since_refresh = now - getattr(self, "_last_refresh", 0)
        ys = [q for q in getattr(self, "_yellow", []) if q[1] < 720]  # never near the card's Plunder! button
        opened = getattr(self, "_opened", [])
        if not ys:
            if since_refresh < 4:                         # trucks are still driving in after a Refresh
                return {"type": "none", "target": "waiting for trucks after Refresh (code)"}
            return ai_action                              # not on the road (or a popup covers it): AI navigates
        fresh = [q for q in ys if not any(abs(q[0] - ox) < 60 and abs(q[1] - oy) < 60 for ox, oy in opened)]
        if fresh:
            x, y = fresh[0]
            return {"type": "tap", "x": x, "y": y, "target": "yellow truck at %d,%d (code)" % (x, y)}
        rxy = getattr(self, "_refresh_xy", None)
        if rxy and since_refresh >= 4:
            self._last_refresh = now
            return {"type": "tap", "x": rxy[0], "y": rxy[1], "target": "Refresh dice (code)"}
        if rxy:
            return {"type": "none", "target": "Refresh cooldown (code)"}
        return ai_action                                  # Refresh not located yet: AI's choice this time
    def _truck_icon(self, act, ans, screen):
        """The trucks button on the base/city view: once a tap really opened the trucks screen, that spot is
        remembered and used every time after, instead of the AI guessing a new place each step."""
        on_road = bool(getattr(self, "_yellow", None)) or bool(ans.get("refresh_xy")) or "truck" in screen.lower() \
            and ("others" in screen.lower() or "road" in screen.lower())
        last = getattr(self, "_icon_try", None)
        if on_road and last:                               # the previous tap opened the trucks screen: learn it
            self.cfg["truck_icon_xy"] = [last[0], last[1]]
            self.cfg.save()
            self._icon_fails = 0
        elif last and last[2]:                             # the learned spot didn't work this time
            self._icon_fails = getattr(self, "_icon_fails", 0) + 1
            if self._icon_fails >= 3:                      # window changed? forget it, the AI looks again
                self.cfg["truck_icon_xy"] = None
                self.cfg.save()
                self._icon_fails = 0
        self._icon_try = None
        tgt = str(act.get("target", "")).lower()
        if act.get("type") == "tap" and "(code)" not in tgt and "truck" in tgt and \
                any(w in tgt for w in ("icon", "button", "round")) and not on_road:
            learned = self.cfg.get("truck_icon_xy")
            used = isinstance(learned, list) and len(learned) == 2
            if used:
                act = dict(act, x=learned[0], y=learned[1], target="trucks button (learned)")
            self._icon_try = (float(act.get("x", 500)), float(act.get("y", 500)), used)
        return act

    def _snap_to_yellow(self, a):
        """When the AI taps a truck, the bot itself taps the middle of the nearest yellow (S) truck that wasn't
        opened yet - so a green/white/blue truck (or an avatar sitting over one) can never be opened."""
        ys = getattr(self, "_yellow", [])
        if not ys:
            return ""
        tgt = str(a.get("target", "")).lower()
        if "(code)" in tgt:
            return ""
        if not any(w in tgt for w in ("truck", "avatar", "yellow", "player", "name tag", "card of")):
            return ""  # buttons, Refresh, Share, X, tabs... are left as the AI chose
        if any(w in tgt for w in ("refresh", "share", "close", " x", "tab", "back", "my truck", "others")):
            return ""
        opened = getattr(self, "_opened", [])
        fresh = [p for p in ys if not any(abs(p[0] - ox) < 60 and abs(p[1] - oy) < 60 for ox, oy in opened)]
        if not fresh:
            return "none-left"
        x, y = float(a.get("x", 500)), float(a.get("y", 500))
        bx, by = min(fresh, key=lambda p: (p[0] - x) ** 2 + (p[1] - y) ** 2)
        a["x"], a["y"] = bx, by
        a["target"] = "yellow truck at %d,%d" % (bx, by)
        return "snapped"

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
            fixed, step = self._prompt_parts()
            try:
                return net.ask_claude(c["claude_key"], model, step, shot, getattr(self, "_card", None), system=fixed)
            except net.ModelGone:
                c["claude_model"] = net.CLAUDE_MODEL
                c.save()
                return net.ask_claude(c["claude_key"], net.CLAUDE_MODEL, step, shot, getattr(self, "_card", None),
                                      system=fixed)
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
            tgt = str(a.get("target", "")).lower()
            if "(code)" not in tgt and "(learned)" not in tgt and any(
                    w in tgt for w in ("world", "alliance", "chat", "send", "channel", "copy", "plunder")):
                return "skip: never send/share to a channel or plunder"
            snapped = self._snap_to_yellow(a)
            if snapped == "none-left":
                return "skip: all yellow trucks already opened - tap Refresh"
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
        if not coords:  # the AI sometimes puts the popup's coordinates on the truck instead of map_coords
            for t in ans.get("trucks") or []:
                coords = _clean_coords((t or {}).get("coords")) if isinstance(t, dict) else ""
                if coords:
                    break
        if coords:
            ans["map_coords"] = coords
        if coords:
            self._send(p, coords)
            self._need_close = True
        elif p["steps"] >= self.PENDING_MAX_STEPS:
            self._send(p, "")

    def _send(self, truck, coords):
        c = self.cfg
        if self._pending is truck:
            self._pending = None
        if c.already_sent(truck["key"]):
            return
        def esc(v):  # names like _x_ or *x* must not break the bold
            return re.sub(r"([*_~|`\\])", r"\\\1", str(v))

        lines = ["SHARD SNIPER — شاحنة S!", "السيرفر: #%d" % truck["server"]]
        if truck["player"]:
            lines.append("اللاعب: " + esc(truck["player"]))
        lines.append("الإحداثيات: " + (esc(coords) if coords else "مش ظاهرة"))
        if truck["arrival"]:
            lines.append("وقت الوصول: " + esc(truck["arrival"]))
        lines.append("شظايا S: %d" % truck["frags"])
        if truck["rewards"]:
            lines.append("الجوايز: " + esc(truck["rewards"]))
        text = "\n\n".join("**%s**" % l for l in lines)  # bold, with an empty line between lines
        sent = False
        if c["discord_webhook"]:
            try:
                net.discord(c["discord_webhook"], text, truck["photo"])
                sent = True
            except Exception as e:
                self._set(status="فشل الإرسال لـ Discord: " + net.short_error(str(e)))
        if c["tg_token"] and c["tg_chat"]:
            try:
                plain = re.sub(r"\\(.)", r"\1", "\n\n".join(lines))  # Telegram is sent without Markdown
                net.telegram(c["tg_token"], c["tg_chat"], plain, truck["photo"])
                sent = True
            except Exception:
                pass
        if sent:
            c["found_count"] = int(c["found_count"]) + 1
            c.save()

    def _prompt(self):
        """Whole prompt in one string (Gemini)."""
        fixed, step = self._prompt_parts()
        return fixed + "\n\n" + step

    def _prompt_parts(self):
        """(fixed, step): the fixed part is identical every step, so Claude can cache it and read it faster;
        the step part is the small text that changes (current task, yellow trucks found, recent steps)."""
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
                "1. From the base/city view click the trucks button: a ROUND icon with a white delivery-truck picture in "
                "the BOTTOM-LEFT corner of the game, directly above the round \"WANTED\" poster icon (around x=60-90, "
                "y=850-900). Target text must contain \"truck icon\". It is NOT on the upper-left, not a building, "
                "not an event icon.\n"
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
                "\nSTRICT RULES - never click: Loot, Plunder, Plunder!, Attack, Deploy, Deploy Escorts, Retry, Buy, "
                "Purchase, Recharge, anything costing diamonds/gold/gems, the OWN truck's refresh button, or chat. "
                "\"Share\" is allowed ONLY to open the popup and read coordinates (then close it with X). If unsure choose \"back\" or \"wait\". \"back\" presses Escape (works in "
                "emulators); in the iPhone app prefer the on-screen X / back arrow. Do not repeat an action that did "
                "not change the screen; try something else.\n\n"
            )
        step = "" if watch else (pending + ("\n" if pending else "") + self._yellow_hint() +
                                 "Recent steps:\n" + hist + "\n")
        return knowledge + task + (
            "Answer ONLY compact JSON, no extra text. Keep \"screen\" to at most 6 words and \"rewards\" to at most 8 words.\n"
            "{\"screen\": \"short description of the current screen\",\n"
            " \"trucks\": [ {\"server\": 398, \"player\": \"name or empty\", \"rank\": \"S|A|B|...\", \"s_fragments\": 3, "
            "\"arrival\": \"04:23:10\", \"coords\": \"X:123 Y:456 if shown on the card, else empty\", "
            "\"rewards\": \"short list\"} ],\n"
            " \"map_coords\": \"X:123 Y:456 if a world map with coordinates is shown, else empty\",\n"
            " \"refresh_xy\": [x, y] of the \"Refresh\" dice button if it is visible (0-1000), else [],\n"
            " \"share_xy\": [x, y] of the blue \"Share\" button on an open truck card, else [],\n"
            " \"popup_close_xy\": [x, y] of the X that closes the Share popup if it is open, else [],\n"
            " \"action\": {\"type\": \"tap|swipe|back|wait\", \"x\": 0-1000, \"y\": 0-1000, \"x2\": 0-1000, \"y2\": 0-1000, "
            "\"target\": \"what you click\"}}\n"
            "x,y are 0-1000 of the screenshot width/height."
        ), step + "Look at the screenshot now and answer with the JSON."

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