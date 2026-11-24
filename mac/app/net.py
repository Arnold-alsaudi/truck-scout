"""Network calls: Gemini (vision), Discord webhook, Telegram (optional), GitHub update check."""
import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _SSL = ssl.create_default_context()

UPDATE_REPO = "Arnold-alsaudi/truck-scout"


class ModelGone(Exception):
    """The configured Gemini model no longer exists for this key."""


class Busy(Exception):
    """Temporary error (rate limit / server); safe to retry."""


def _request(url, data=None, headers=None, method=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def retry(fn, tries=4):
    last = None
    for i in range(tries):
        try:
            return fn()
        except (Busy, urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def short_error(body):
    try:
        msg = json.loads(body)["error"]["message"]
    except Exception:
        msg = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    msg = re.sub(r"\s+", " ", msg)
    return msg[:140] + ("…" if len(msg) > 140 else "")


# ---------------------------------------------------------------- Gemini

def ask_gemini(key, model, prompt, jpeg):
    def once():
        body = {
            "contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
            ]}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0.2},
        }
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % urllib.parse.quote(model)
        code, resp = _request(url, json.dumps(body).encode(), {
            "Content-Type": "application/json", "x-goog-api-key": key}, "POST", 90)
        if code == 429 or code >= 500:
            time.sleep(10)
            raise Busy("Gemini %d" % code)
        if code == 404:
            raise ModelGone(model)
        if code >= 400:
            raise RuntimeError("Gemini %d: %s" % (code, short_error(resp)))
        text = json.loads(resp)["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = re.sub(r"^```(json)?|```$", "", text).strip()
        return json.loads(text)
    return retry(once)


def pick_flash_model(key):
    """Newest stable 'flash' model this key can use (previews only as a fallback)."""
    code, resp = _request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                          headers={"x-goog-api-key": key}, timeout=30)
    if code >= 400:
        raise RuntimeError("Gemini %d: %s" % (code, short_error(resp)))
    best, best_v, prev, prev_v = None, -1.0, None, -1.0
    for m in json.loads(resp).get("models", []):
        name = m.get("name", "").replace("models/", "")
        low = name.lower()
        if "flash" not in low or re.search(r"lite|image|tts|live|audio|embed|thinking|8b", low):
            continue
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        mv = re.search(r"gemini-(\d+(?:\.\d+)?)", low)
        v = float(mv.group(1)) if mv else 0.0
        if re.search(r"preview|exp|-\d{2}-\d{2}|-\d{3,}", low):
            if v > prev_v:
                prev, prev_v = name, v
        elif v > best_v or (v == best_v and best and len(name) < len(best)):
            best, best_v = name, v
    best = best or prev
    if not best:
        raise RuntimeError("مفيش موديل Gemini Flash متاح للمفتاح ده")
    return best


# ---------------------------------------------------------------- Discord / Telegram

def _multipart(fields, file_field=None, filename=None, file_bytes=None, file_type="image/jpeg"):
    b = "----ss" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n" % (b, k)).encode()
        if k == "payload_json":
            out += b"Content-Type: application/json\r\n"
        out += b"\r\n" + v.encode() + b"\r\n"
    if file_bytes is not None:
        out += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                % (b, file_field, filename, file_type)).encode()
        out += file_bytes + b"\r\n"
    out += ("--%s--\r\n" % b).encode()
    return bytes(out), "multipart/form-data; boundary=" + b


def discord(webhook, text, jpeg=None):
    def once():
        payload = json.dumps({"content": text, "username": "Saad Shard Sniper"})
        body, ctype = _multipart({"payload_json": payload}, "files[0]", "truck.jpg", jpeg)
        url = webhook + ("&" if "?" in webhook else "?") + "wait=true"
        code, resp = _request(url, body, {"Content-Type": ctype, "User-Agent": "SaadShardSniper (mac, 1.0)"}, "POST")
        if code == 429:
            try:
                time.sleep(min(float(json.loads(resp).get("retry_after", 5)), 60))
            except Exception:
                time.sleep(5)
            raise Busy("Discord 429")
        if code >= 500:
            raise Busy("Discord %d" % code)
        if code >= 400:
            raise RuntimeError("Discord %d: %s" % (code, short_error(resp)))
    retry(once)


def telegram(token, chat, text, jpeg=None):
    def once():
        if jpeg is None:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            code, resp = _request("https://api.telegram.org/bot%s/sendMessage" % token, data,
                                  {"Content-Type": "application/x-www-form-urlencoded"}, "POST")
        else:
            body, ctype = _multipart({"chat_id": chat, "caption": text}, "photo", "truck.jpg", jpeg)
            code, resp = _request("https://api.telegram.org/bot%s/sendPhoto" % token, body,
                                  {"Content-Type": ctype}, "POST")
        if code == 429:
            try:
                time.sleep(min(json.loads(resp)["parameters"]["retry_after"], 90))
            except Exception:
                time.sleep(5)
            raise Busy("Telegram 429")
        if code >= 500:
            raise Busy("Telegram %d" % code)
        if code >= 400:
            raise RuntimeError("Telegram %d: %s" % (code, short_error(resp)))
    retry(once)


# ---------------------------------------------------------------- license

def fetch_license(client_id):
    """The owner's license entry for this client from license.json in the repo.
    Returns a dict (status/pass_sha256/message), {} if the client has no entry, or None if offline."""
    code, resp = _request("https://api.github.com/repos/%s/contents/license.json?ref=main&t=%d"
                          % (UPDATE_REPO, int(time.time())),
                          headers={"Accept": "application/vnd.github.raw+json", "Cache-Control": "no-cache"},
                          timeout=15)
    if code not in (200, 404):
        # API refused (e.g. rate limit): read the same file from GitHub's file server instead
        code, resp = _request("https://raw.githubusercontent.com/%s/main/license.json?t=%d"
                              % (UPDATE_REPO, int(time.time())), headers={"Cache-Control": "no-cache"}, timeout=15)
    if code == 404:
        return {}
    if code != 200:
        return None
    try:
        return (json.loads(resp).get("clients") or {}).get(client_id) or {}
    except ValueError:
        return None


# ---------------------------------------------------------------- updates

def newer_release(current_version, prefix="mac-v"):
    """Returns (version, page_url) of a newer release with tags <prefix><N>, or None."""
    code, resp = _request("https://api.github.com/repos/%s/releases?per_page=30" % UPDATE_REPO,
                          headers={"Accept": "application/vnd.github+json"}, timeout=20)
    if code != 200:
        return None
    best = None
    for rel in json.loads(resp):
        m = re.match(re.escape(prefix) + r"(\d+)$", rel.get("tag_name", ""))
        if m and int(m.group(1)) > current_version and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), rel.get("html_url"))
    return best
