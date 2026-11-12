"""Atenea/Hermes configuration in ~/.config/atenea/config.json (editable from the web panel)."""
from __future__ import annotations

import json
import os

_NEW, _OLD = os.path.expanduser("~/.config/atenea/config.json"), os.path.expanduser("~/.config/athena/config.json")
PATH = os.path.expanduser(os.environ.get("ATENEA_CONFIG") or os.environ.get("ATHENA_CONFIG")
                          or (_OLD if os.path.exists(_OLD) and not os.path.exists(_NEW) else _NEW))

DEFAULTS = {
    "assistant_name": "Atenea",
    "model": {"url": "http://127.0.0.1:8080/v1/chat/completions", "model": "ornith", "temperature": 1.0},
    "telegram": {"token": "", "allowed_users": []},
    "workspace": "~/projects",
    "projects": {},                 # alias -> path
    "skills_dirs": ["~/.config/atenea/skills"],
    "mcp_servers": {},              # name -> {"command", "args", "env", "url", "dangerous": [...], "enabled": true}
    "confirm_tools": [],            # tools that always need confirmation (on top of the dangerous ones)
    "verify_default": "",
    "web": {"host": "0.0.0.0", "port": 8787, "password": ""},
    "instructions": "Be brief and direct.",
}


def load() -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(PATH):
        user = json.load(open(PATH))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save(cfg: dict):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    json.dump(cfg, open(tmp, "w"), ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, PATH)
