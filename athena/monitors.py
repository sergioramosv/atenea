"""Monitors: watch a command, a background process or a log and wake Atenea when something happens.

Examples Atenea can set up:
  - "tell me when the build finishes"         -> watch a background process until it exits
  - "if ERROR shows up in app.log, look at it" -> run `tail -n 200 app.log` every 30 s, pattern "ERROR"
  - "check every 5 min whether the site is up" -> command `curl -sf https://...`, trigger on failure
When a monitor fires, `on_fire(monitor, text)` is called (the web panel posts it into the conversation)."""
from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid

from . import background as BG
from .registry import Tool

MONITORS: dict[str, dict] = {}
on_fire = None                       # set by the host: on_fire(monitor: dict, text: str)


def _fire(m: dict, text: str):
    m["hits"].append({"t": time.time(), "text": text[-3000:]})
    m["fired"] += 1
    if m["once"]:
        m["status"] = "fired"
    if on_fire:
        try:
            on_fire(m, text)
        except Exception as e:
            print(f"[monitor] on_fire failed: {e}", flush=True)


def _loop(m: dict):
    rx = re.compile(m["pattern"], re.I | re.M) if m["pattern"] else None
    seen = 0                                            # bytes of the process log already read
    last_out = None
    while m["status"] == "watching":
        try:
            if m["process"]:
                i = BG.PROCS.get(m["process"])
                if not i:
                    _fire(m, f"Background process {m['process']} no longer exists.")
                    break
                data = open(i["log"], "rb").read()
                new, seen = data[seen:].decode(errors="replace"), len(data)
                if rx and new:
                    hits = [ln for ln in new.splitlines() if rx.search(ln)]
                    if hits:
                        _fire(m, "Matching lines:\n" + "\n".join(hits[-30:]))
                rc = i["proc"].poll()
                if rc is not None and m["status"] == "watching":
                    tail = "\n".join(data.decode(errors="replace").splitlines()[-40:])
                    if not rx or m.get("on_exit", True):
                        _fire(m, f"Process '{i['name']}' finished with exit code {rc}. Last output:\n{tail}")
                    m["status"] = "done"
                    break
            else:
                r = subprocess.run(m["command"], shell=True, cwd=m["cwd"], capture_output=True, text=True,
                                   timeout=max(30, m["interval"] * 2), executable="/bin/bash")
                out = (r.stdout + r.stderr)[-6000:]
                m["last_check"], m["last_rc"] = time.time(), r.returncode
                if rx:
                    if rx.search(out) and (not m["changes_only"] or out != last_out):
                        _fire(m, f"`{m['command']}` matched /{m['pattern']}/:\n{out[-2500:]}")
                elif m["trigger"] == "success" and r.returncode == 0:
                    _fire(m, f"`{m['command']}` succeeded:\n{out[-2500:]}")
                elif m["trigger"] == "failure" and r.returncode != 0:
                    _fire(m, f"`{m['command']}` failed (exit {r.returncode}):\n{out[-2500:]}")
                elif m["trigger"] == "change" and last_out is not None and out != last_out:
                    _fire(m, f"The output of `{m['command']}` changed:\n{out[-2500:]}")
                last_out = out
        except Exception as e:
            m["error"] = str(e)[:300]
        if time.time() > m["expires"]:
            m["status"] = "expired"
            break
        for _ in range(int(m["interval"])):
            if m["status"] != "watching":
                break
            time.sleep(1)


def start(name: str, cwd: str, chat: str | None = None, command: str = "", process: str = "", pattern: str = "",
          trigger: str = "", interval: int = 30, once: bool = True, hours: float = 24, changes_only: bool = True) -> dict:
    if not command and not process:
        raise ValueError("give a `command` to run periodically or a background `process` id to watch")
    if process and process not in BG.PROCS:
        raise ValueError(f"no background process {process}")
    if pattern:
        re.compile(pattern)
    m = {"id": uuid.uuid4().hex[:6], "name": name or (command or process)[:40], "chat": chat, "cwd": cwd,
         "command": command, "process": process, "pattern": pattern,
         "trigger": trigger or ("" if pattern else "exit" if process else "failure"),
         "interval": max(5, int(interval or 30)), "once": bool(once), "changes_only": changes_only,
         "status": "watching", "created": time.time(), "expires": time.time() + max(0.1, hours) * 3600,
         "fired": 0, "hits": [], "last_check": None, "last_rc": None, "error": None}
    MONITORS[m["id"]] = m
    threading.Thread(target=_loop, args=(m,), daemon=True).start()
    return m


def stop(mid: str) -> str:
    m = MONITORS.get(mid)
    if not m:
        return f"No monitor {mid}."
    if m["status"] == "watching":
        m["status"] = "stopped"
    return f"Stopped monitor {mid} ({m['name']})."


def listing(chat: str | None = None) -> list[dict]:
    return [m for m in MONITORS.values() if chat is None or m["chat"] == chat]


def describe(m: dict) -> str:
    what = f"process {m['process']}" if m["process"] else f"`{m['command']}` every {m['interval']}s"
    cond = f"/{m['pattern']}/" if m["pattern"] else m["trigger"]
    return f"{m['id']}  {m['status']:<9} {m['name']} · {what} · when {cond} · fired {m['fired']}×"


def tools(cwd_fn, chat: str | None = None) -> list[Tool]:
    def monitor_start(name: str, command: str = "", process_id: str = "", pattern: str = "", trigger: str = "",
                      interval_seconds: int = 30, once: bool = True, hours: float = 24) -> str:
        try:
            m = start(name, cwd_fn(), chat, command, process_id, pattern, trigger, interval_seconds, once, hours)
        except (ValueError, re.error) as e:
            return f"Error: {e}"
        return (f"Monitor started: {describe(m)}\nWhen it fires you will receive a message in this conversation "
                "with what happened; then act on it and tell the user. You can now finish your reply.")

    return [
        Tool("monitor_start", "Watch something in the background and get woken up when it happens. Either `command` "
             "(run every interval_seconds; fires when `pattern` regex matches its output, or by `trigger`: "
             "'failure' (default), 'success' or 'change') or `process_id` of a bg_start process (fires on lines "
             "matching `pattern` and when it exits). once=true stops after the first time. Use it for 'tell me when "
             "X finishes', 'watch this log for errors', 'check every N minutes whether…'.",
             {"type": "object", "properties": {
                 "name": {"type": "string"}, "command": {"type": "string"}, "process_id": {"type": "string"},
                 "pattern": {"type": "string"}, "trigger": {"type": "string", "enum": ["failure", "success", "change"]},
                 "interval_seconds": {"type": "integer"}, "once": {"type": "boolean"},
                 "hours": {"type": "number", "description": "stop watching after this many hours (default 24)"}},
              "required": ["name"]}, monitor_start),
        Tool("monitor_list", "List monitors and their status.", {"type": "object", "properties": {}},
             lambda: "\n".join(describe(m) for m in listing()) or "No monitors."),
        Tool("monitor_stop", "Stop a monitor.", {"type": "object", "properties": {"id": {"type": "string"}},
                                                 "required": ["id"]}, lambda id: stop(id)),
    ]
