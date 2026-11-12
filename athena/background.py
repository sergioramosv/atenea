"""Background processes: dev servers, long scripts, watchers. Atenea starts them, keeps working, and reads their logs."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid

from . import config as C
from .registry import Tool

DIR = os.path.join(os.path.dirname(C.PATH), "background")
PROCS: dict[str, dict] = {}
_lock = threading.Lock()


def start(command: str, cwd: str, name: str = "") -> dict:
    os.makedirs(DIR, exist_ok=True)
    pid_id = uuid.uuid4().hex[:6]
    log = os.path.join(DIR, f"{pid_id}.log")
    f = open(log, "wb")
    p = subprocess.Popen(command, shell=True, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True, executable="/bin/bash")
    info = {"id": pid_id, "name": name or command[:40], "command": command, "cwd": cwd, "pid": p.pid,
            "started": time.time(), "log": log, "proc": p}
    with _lock:
        PROCS[pid_id] = info
    return info


def status(i: dict) -> str:
    rc = i["proc"].poll()
    return "running" if rc is None else f"exited ({rc})"


def tail(pid_id: str, lines: int = 60) -> str:
    i = PROCS.get(pid_id)
    if not i:
        return f"Error: no background process {pid_id}. Use bg_list."
    try:
        data = open(i["log"], "rb").read()[-200_000:].decode(errors="replace")
    except OSError:
        data = ""
    out = "\n".join(data.splitlines()[-max(1, min(int(lines or 60), 400)):])
    return f"[{pid_id} · {i['name']} · {status(i)}]\n{out or '(no output yet)'}"


def stop(pid_id: str) -> str:
    i = PROCS.get(pid_id)
    if not i:
        return f"Error: no background process {pid_id}."
    if i["proc"].poll() is None:
        try:
            os.killpg(i["proc"].pid, signal.SIGTERM)
            i["proc"].wait(5)
        except Exception:
            try:
                os.killpg(i["proc"].pid, signal.SIGKILL)
            except Exception:
                pass
    return f"Stopped {pid_id} ({i['name']})."


def listing() -> list[dict]:
    return [{k: v for k, v in i.items() if k != "proc"} | {"status": status(i)} for i in PROCS.values()]


def tools(cwd_fn) -> list[Tool]:
    def bg_start(command: str, name: str = "") -> str:
        i = start(command, cwd_fn(), name)
        time.sleep(2)
        return f"Started background process {i['id']} (pid {i['pid']}).\n" + tail(i["id"], 20) + \
            "\nUse bg_logs to check it later and bg_stop when it is no longer needed."

    def bg_list() -> str:
        rows = listing()
        return "\n".join(f"{r['id']}  {r['status']:<12} {r['name']}  (cwd {r['cwd']})" for r in rows) or "No background processes."

    return [
        Tool("bg_start", "Run a long-running command in the background (dev server, watcher, long script) and keep "
             "working. Returns an id and the first output lines.",
             {"type": "object", "properties": {"command": {"type": "string"}, "name": {"type": "string"}},
              "required": ["command"]}, bg_start),
        Tool("bg_logs", "Show the latest output of a background process.",
             {"type": "object", "properties": {"id": {"type": "string"}, "lines": {"type": "integer"}},
              "required": ["id"]}, lambda id, lines=60: tail(id, lines)),
        Tool("bg_stop", "Stop a background process.",
             {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, lambda id: stop(id)),
        Tool("bg_list", "List background processes and their status.", {"type": "object", "properties": {}}, bg_list),
    ]
