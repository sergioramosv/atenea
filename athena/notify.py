"""Push notifications to Sergio's phone through the Telegram bot (when it is configured)."""
from __future__ import annotations

import json
import threading
import urllib.request

from . import config as C


def send(text: str, url: str | None = None):
    """Fire-and-forget Telegram message to every allowed user. No-op when Telegram is not set up."""
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
