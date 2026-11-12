"""Atenea Bóveda: encrypted secrets (user names, passwords, tokens) for flows.

Secrets never go into a prompt, a log or a chat. Flows read them by name at the moment they type them into a page or
a request header; every text a flow saves passes through `redact()` first.

Storage: ~/.config/atenea/vault.json holds Fernet-encrypted values; the key is ~/.config/atenea/vault.key (0600).
The nightly backup copies the encrypted file, not the key."""
from __future__ import annotations

import json
import os
import threading
import time

from cryptography.fernet import Fernet

from . import config as C

DIR = os.path.dirname(C.PATH)
PATH = os.path.join(DIR, "vault.json")
KEY = os.environ.get("ATENEA_VAULT_KEY_FILE") or os.path.join(DIR, "vault.key")
_lock = threading.Lock()


def _fernet() -> Fernet:
    if not os.path.exists(KEY):
        os.makedirs(os.path.dirname(KEY), exist_ok=True)
        fd = os.open(KEY, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    return Fernet(open(KEY, "rb").read().strip())


def _load() -> dict:
    try:
        return json.load(open(PATH, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(d: dict):
    tmp = PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, PATH)


def put(name: str, fields: dict[str, str], domains: list[str] | None = None, label: str = ""):
    """Create or update an entry. Empty field values keep the stored value (so the UI never needs to read them)."""
    f = _fernet()
    with _lock:
        d = _load()
        e = d.get(name) or {"fields": {}, "created": time.time()}
        for k, v in fields.items():
            if v:
                e["fields"][k] = f.encrypt(v.encode()).decode()
        if domains is not None:
            e["domains"] = [x.strip().lower() for x in domains if x.strip()]
        if label:
            e["label"] = label
        e["updated"] = time.time()
        d[name] = e
        _save(d)


def get(name: str, field: str) -> str | None:
    e = _load().get(name)
    if not e or field not in e.get("fields", {}):
        return None
    return _fernet().decrypt(e["fields"][field].encode()).decode()


def has(name: str, *fields: str) -> bool:
    e = _load().get(name) or {}
    return all(k in e.get("fields", {}) for k in fields)


def listing() -> list[dict]:
    """Names, labels, field names and dates: never values."""
    return [{"name": n, "label": e.get("label", ""), "fields": sorted(e.get("fields", {})), "domains": e.get("domains", []),
             "updated": e.get("updated")} for n, e in sorted(_load().items())]


def delete(name: str) -> bool:
    with _lock:
        d = _load()
        if d.pop(name, None) is None:
            return False
        _save(d)
        return True


def redact(text: str) -> str:
    """Hide every stored secret (passwords, tokens; values of 4+ characters) inside a text."""
    if not text:
        return text
    d = _load()
    if not d:
        return text
    f = _fernet()
    for e in d.values():
        for k, v in e.get("fields", {}).items():
            if k in ("user", "username", "email"):
                continue
            try:
                s = f.decrypt(v.encode()).decode()
            except Exception:
                continue
            if len(s) >= 4:
                text = text.replace(s, "••••")
    return text
