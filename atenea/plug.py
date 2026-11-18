"""Smart plug (Tapo P100) as a tool for Atenea: "apaga el enchufe", "enciéndelo", "¿está encendido?".

The account (e-mail + password of the Tapo app) lives in Atenea Bóveda under the name "tapo"; the plug's address is
config["plug"]["ip"]. The `tapo` library needs Python 3.12 and speaks the newer TPAP protocol, so it runs in its own
venv through servicios/enchufe/tapo_ctl.py. The model never sees the credentials."""
from __future__ import annotations

import json
import os
import subprocess

from . import config as C
from . import vault as V
from .registry import Tool

PY = os.environ.get("ATENEA_TAPO_PY", "/mnt/models/tools/tapo-venv/bin/python")
CTL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "servicios", "enchufe", "tapo_ctl.py")


def configured() -> bool:
    return bool((C.load().get("plug") or {}).get("ip")) and V.has("tapo", "user", "password") and os.path.exists(PY)


def control(action: str) -> dict:
    env = {**os.environ, "TAPO_USER": V.get("tapo", "user") or "", "TAPO_PASS": V.get("tapo", "password") or "",
           "TAPO_IP": C.load()["plug"]["ip"]}
    r = subprocess.run([PY, CTL, action], env=env, capture_output=True, text=True, timeout=60)
    try:
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"error": (r.stderr or "no answer").strip()[-200:]}


def smart_plug(action: str = "status") -> str:
    if action not in ("status", "on", "off"):
        return "Error: action must be status, on or off."
    r = control(action)
    if r.get("error"):
        return f"Error: could not reach the plug ({V.redact(r['error'])})."
    state = "encendido" if r["on"] else "apagado"
    if action != "status" and r["on"] != (action == "on"):
        return f"Error: I sent '{action}' but the plug is still {state}."
    return f"El enchufe «{r.get('name') or 'enchufe'}» está {state}."


def tools() -> list[Tool]:
    if not configured():
        return []
    return [Tool("smart_plug", "Turn Sergio's smart plug on or off, or ask whether it is on. `action`: on, off or status. "
                 "Use it for 'enciende/apaga el enchufe'.",
                 {"type": "object", "properties": {"action": {"type": "string", "enum": ["status", "on", "off"]}},
                  "required": ["action"]}, smart_plug)]
