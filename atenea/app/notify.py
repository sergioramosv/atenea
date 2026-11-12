"""Notifications to Sergio's phone: Web Push (the Atenea home-screen app / browser) and Telegram (when configured)."""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request

from .. import config as C

SUBS = os.path.join(os.path.dirname(C.PATH), "push.json")
LAST_SEEN = {"t": 0.0}          # last time a panel tab reported it was visible: skip pushes while you are looking
_lock = threading.Lock()


def vapid() -> dict:
    """VAPID keys for Web Push, generated once and stored in config["web"]["vapid"]."""
    cfg = C.load()
    v = cfg["web"].get("vapid")
    if not v:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        k = ec.generate_private_key(ec.SECP256R1())
        pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode()
        pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        v = {"private_pem": pem, "public": base64.urlsafe_b64encode(pub).decode().rstrip("=")}
        cfg["web"]["vapid"] = v
        C.save(cfg)
    return v


def subs() -> list[dict]:
    try:
        return json.load(open(SUBS))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_subs(items: list[dict]):
    json.dump(items, open(SUBS, "w"), indent=1)
    os.chmod(SUBS, 0o600)


def subscribe(sub: dict, label: str = "") -> int:
    with _lock:
        items = [s for s in subs() if s["sub"].get("endpoint") != sub.get("endpoint")]
        items.append({"sub": sub, "label": label[:80], "added": time.time()})
        save_subs(items)
        return len(items)


def unsubscribe(endpoint: str):
    with _lock:
        save_subs([s for s in subs() if s["sub"].get("endpoint") != endpoint])


def webpush(title: str, body: str, url: str = "/", tag: str | None = None, force: bool = False) -> int:
    """Send to every subscribed device; drops subscriptions the browser has revoked. Returns how many were sent."""
    title, body = no_emoji(title), no_emoji(body)
    if not force and time.time() - LAST_SEEN["t"] < 25:
        return 0
    try:
        from pywebpush import WebPushException, webpush as wp
    except ImportError:
        return 0
    v, sent, dead = vapid(), 0, []
    from py_vapid import Vapid
    key = Vapid.from_pem(v["private_pem"].encode())
    for s in subs():
        try:
            wp(subscription_info=s["sub"], data=json.dumps({"title": title, "body": body[:400], "url": url, "tag": tag}),
               vapid_private_key=key, vapid_claims={"sub": "mailto:sergioramosv@users.noreply.github.com"}, ttl=3600)
            sent += 1
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                dead.append(s["sub"].get("endpoint"))
            else:
                print(f"[notify] web push failed: {e}", flush=True)
        except Exception as e:
            print(f"[notify] web push failed: {e}", flush=True)
    for ep in dead:
        unsubscribe(ep)
    return sent


_EMOJI = None


def no_emoji(text: str) -> str:
    """Sergio does not want emojis anywhere: strip them from anything sent out."""
    global _EMOJI
    import re
    if _EMOJI is None:
        _EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u231A\u231B\u23E9-\u23FA\uFE0F\u200D]+ ?")
    return _EMOJI.sub("", text or "")


def send(text: str, url: str | None = None, title: str = "Atenea"):
    """Fire-and-forget: Web Push to subscribed devices and Telegram to allowed users (each only if set up)."""
    path = "/" + url.split("/", 3)[3] if url and url.count("/") >= 3 else "/"
    threading.Thread(target=webpush, args=(title, text, path), daemon=True).start()
    return send_telegram(f"{title}\n{text}" if title and title != "Atenea" else text, url)


def send_telegram(text: str, url: str | None = None):
    """Telegram message to every allowed user. No-op when Atenea Telegram is not set up."""
    text = no_emoji(text)
    tg = C.load().get("telegram") or {}
    token, users = tg.get("token"), tg.get("allowed_users") or []
    if not token or not users:
        return False

    def go():
        body = text[:3800] + (f"\n\n{url}" if url else "")
        for u in users:
            try:
                req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                             json.dumps({"chat_id": u, "text": body,
                                                         "disable_web_page_preview": True}).encode(),
                                             {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=15).read()
            except Exception as e:
                print(f"[notify] telegram failed: {e}", flush=True)
    threading.Thread(target=go, daemon=True).start()
    return True
