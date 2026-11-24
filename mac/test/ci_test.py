"""End-to-end test of the Mac engine on a real Mac (GitHub runner).

A Preview window titled "Last Z.png" stands in for the game. Gemini and Discord are replaced by fakes so no
keys are needed; everything else (window search, screenshot, clicks, the loop, truck filtering, alert text)
is the real code.
"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app"))
OUT = os.path.join(HERE, "out")
os.makedirs(OUT, exist_ok=True)

import macos  # noqa: E402
import net    # noqa: E402
import engine  # noqa: E402

results = {}
ok = True


def check(name, cond, detail=""):
    global ok
    results[name] = {"pass": bool(cond), "detail": detail}
    print(("PASS " if cond else "FAIL ") + name + (" — " + str(detail) if detail else ""))
    ok = ok and bool(cond)


# 1. window search
wins = macos.list_windows()
check("list_windows", len(wins) > 0, [w["label"] for w in wins][:8])
g = macos.guess_game_window()
check("guess_game_window finds 'Last Z'", g is not None and "last z" in g["label"].lower(), g and g["label"])

# 2. screenshot of just that window
info = macos.window_info(g["id"]) if g else None
check("window_info", info is not None and info["w"] > 100, info)
shot = macos.capture_jpeg(g["id"]) if g else None
check("capture_jpeg", shot is not None and len(shot) > 5000, shot and len(shot))
if shot:
    with open(os.path.join(OUT, "capture_small.jpg"), "wb") as f:
        f.write(shot)
full = macos.capture_full_jpeg(g["id"]) if g else None
if full:
    with open(os.path.join(OUT, "capture_full.jpg"), "wb") as f:
        f.write(full)

# 3. clicks land where we aim
if info:
    macos.activate(info["pid"])
    time.sleep(0.5)
    x, y = macos.to_screen(info, 500, 500)
    macos.click(x, y)
    time.sleep(0.2)
    mx, my = macos.mouse_position()
    check("click moves mouse to target", abs(mx - x) < 4 and abs(my - y) < 4, (round(x), round(y), round(mx), round(my)))
    x2, y2 = macos.to_screen(info, 500, 300)
    macos.drag(x, y, x2, y2)
    mx, my = macos.mouse_position()
    check("drag ends at target", abs(mx - x2) < 4 and abs(my - y2) < 4, (round(x2), round(y2), round(mx), round(my)))

# 4. whole loop with fake Gemini + fake Discord
sent = []
asked = []


def fake_gemini(key, model, prompt, jpeg):
    asked.append({"model": model, "prompt_has_servers": "#385" in prompt, "jpeg": len(jpeg),
                  "asks_view": "tap \"Share\" on its card" in prompt})
    if len(asked) >= 2:
        # second step: the bot pressed View, the world map shows the truck's coordinates
        return {"screen": "world map at truck", "trucks": [], "map_coords": "#398 X:512 Y:733",
                "action": {"type": "back"}}
    return {
        "screen": "truck card",
        "trucks": [
            {"server": 398, "player": "ToE-snowsFarm", "rank": "S", "s_fragments": 3, "arrival": "04:23:10",
             "coords": "", "rewards": "3x S fragment, diamonds"},
            {"server": 405, "player": "Excluded", "rank": "S", "s_fragments": 3},   # excluded server
            {"server": 398, "player": "LowRank", "rank": "A", "s_fragments": 3},     # not S
        ],
        "action": {"type": "tap", "x": 500, "y": 920, "target": "close"},
    }


net.ask_gemini = fake_gemini
net.pick_flash_model = lambda key: "gemini-test-flash"
net.discord = lambda url, text, jpeg=None: sent.append({"text": text, "photo": len(jpeg or b"")})

states = []
e = engine.Engine(lambda s: states.append(s))
e.cfg.update({"gemini_key": "test", "discord_webhook": "https://example.invalid/webhook", "interval": 3,
              "seen": {}, "found_count": 0, "window_id": 0})
e.start()
time.sleep(12)
e.stop()
check("after finding S truck, prompt asks to press View", len(asked) >= 2 and asked[1]["asks_view"], asked[:2])
check("alert carries map coordinates", bool(sent) and "X:512 Y:733" in sent[0]["text"], sent and sent[0]["text"])
check("engine started", any(s["state"] == "running" for s in states), [s["status"] for s in states][:6])
check("gemini called with screenshot", len(asked) >= 1 and asked[0]["jpeg"] > 5000, asked[:1])
check("auto model used", asked and asked[0]["model"] == "gemini-test-flash", asked and asked[0]["model"])
check("exactly one S truck sent (filters work)", len(sent) == 1, [s["text"].splitlines()[1] for s in sent])
check("alert has server + player + photo",
      bool(sent) and "#398" in sent[0]["text"] and "ToE-snowsFarm" in sent[0]["text"] and sent[0]["photo"] > 5000)
check("same truck not sent twice", len(sent) == 1)
if sent:
    with open(os.path.join(OUT, "alert.txt"), "w", encoding="utf-8") as f:
        f.write(sent[0]["text"])

# 5. license: real fetch, then each state with a fake license file
import hashlib  # noqa: E402
import main     # noqa: E402

real = net.fetch_license(main.CLIENT_ID)
check("license.json reachable", real is not None, real)

api = main.Api()
api._engine.cfg.update({"lic": {}, "lic_checked": 0, "unlock_hash": ""})
fake = {"value": {"status": "active"}}
net.fetch_license = lambda cid: fake["value"]

check("active -> runs", api.license()["status"] == "active")
fake["value"] = {"status": "disabled", "message": "pay first"}
lic = api.license()
check("disabled -> blocked with message", lic["status"] == "disabled" and lic["message"] == "pay first", lic)
fake["value"] = {"status": "locked", "pass_sha256": hashlib.sha256(b"1234").hexdigest()}
check("locked -> asks password", api.license()["status"] == "locked")
check("wrong password refused", api.unlock("9999") is False and api.license()["status"] == "locked")
check("right password opens", api.unlock("1234") is True and api.license()["status"] == "active")
fake["value"] = {"status": "locked", "pass_sha256": hashlib.sha256(b"new").hexdigest()}
check("new password locks again", api.license()["status"] == "locked")
fake["value"] = {"status": "active"}
api.license()
api._engine.cfg["gemini_key"] = "test"
api._engine.start()
fake["value"] = {"status": "disabled"}
api._apply_license(api._license_now())
check("disabling stops a running bot", api._engine.running is False)
fake["value"] = None  # offline
api._engine.cfg["lic_checked"] = time.time() - 3600
check("offline 1h -> still works", api.license()["status"] in ("disabled", "active"))
api._engine.cfg["lic_checked"] = time.time() - 49 * 3600
check("offline 49h -> locked", api.license()["status"] == "offline")
api._engine.cfg.update({"lic": {}, "lic_checked": 0, "unlock_hash": ""})
api._engine.cfg.save()

with open(os.path.join(OUT, "results.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=1, default=str)
print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
