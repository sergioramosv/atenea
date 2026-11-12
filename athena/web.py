"""Athena web panel: chat, configuration and status.  Run: python -m athena.web
HTTP Basic auth (user: sergio, password: config["web"]["password"], generated if empty)."""
from __future__ import annotations

import os
import secrets
import threading
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from . import config as C
from .runtime import build_agent

app = FastAPI(title="Athena")
security = HTTPBasic()
JOBS: dict[str, dict] = {}
AGENTS: dict[str, object] = {}          # one web conversation per project


def auth(cred: HTTPBasicCredentials = Depends(security)):
    pw = C.load()["web"].get("password", "")
    if not (cred.username == "sergio" and pw and secrets.compare_digest(cred.password, pw)):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})


class ChatIn(BaseModel):
    text: str
    project: str | None = None
    new: bool = False


def _run_job(jid: str, text: str, project: str | None, new: bool):
    job = JOBS[jid]
    cfg = C.load()
    cwd = os.path.expanduser(cfg.get("projects", {}).get(project or "", project or "") or cfg["workspace"])

    def ev(kind, data):
        if kind in ("tool_call", "notice", "subagent", "text"):
            job["events"].append({"t": round(time.time() - job["t0"], 1), "kind": kind,
                                  "data": {k: str(v)[:400] for k, v in data.items()}})

    def approve(tool, args):                       # confirmation from the web page
        job["approval"] = {"tool": tool.name, "args": str(args)[:1500], "answer": None}
        for _ in range(600):
            if job["approval"]["answer"] is not None:
                ok = job["approval"]["answer"]
                job["approval"] = None
                return ok
            time.sleep(1)
        job["approval"] = None
        return False
    try:
        if new or cwd not in AGENTS:
            AGENTS[cwd] = build_agent(cwd)
        ag = AGENTS[cwd]
        ag.on_event, ag.approve = ev, approve
        job["result"] = ag.run(text)
        job["stats"] = dict(ag.stats.__dict__)
    except Exception as e:
        job["result"] = f"Error: {type(e).__name__}: {e}"
    job["done"] = True


@app.post("/api/chat", dependencies=[Depends(auth)])
def chat(inp: ChatIn):
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"t0": time.time(), "events": [], "done": False, "result": None, "approval": None}
    threading.Thread(target=_run_job, args=(jid, inp.text, inp.project, inp.new), daemon=True).start()
    return {"job": jid}


@app.get("/api/jobs/{jid}", dependencies=[Depends(auth)])
def job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    j = JOBS[jid]
    return {k: j[k] for k in ("events", "done", "result", "approval")} | {"secs": round(time.time() - j["t0"])}


@app.post("/api/jobs/{jid}/approve/{answer}", dependencies=[Depends(auth)])
def approve(jid: str, answer: str):
    a = JOBS.get(jid, {}).get("approval")
    if a:
        a["answer"] = answer == "yes"
    return {"ok": bool(a)}


@app.get("/api/config", dependencies=[Depends(auth)])
def get_config():
    cfg = C.load()
    if cfg["telegram"].get("token"):
        cfg["telegram"]["token"] = cfg["telegram"]["token"][:8] + "…(hidden)"
    cfg["web"]["password"] = "…(hidden)"
    return cfg


@app.put("/api/config", dependencies=[Depends(auth)])
def put_config(new: dict):
    cfg = C.load()
    if "…(hidden)" in str(new.get("telegram", {}).get("token", "")):
        new.setdefault("telegram", {})["token"] = cfg["telegram"].get("token", "")
    new.pop("web", None)                           # the password is not changed from the browser
    cfg.update(new)
    C.save(cfg)
    AGENTS.clear()
    return {"ok": True}


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    import subprocess

    def sh(c):
        return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
    return {"ornith": sh("systemctl is-active ornith"), "hermes": sh("systemctl is-active athena-hermes"),
            "ram": sh("free -g | awk '/Mem/{print $3\"/\"$2\" GB\"}'"), "gpu_temp": sh("sensors | awk '/edge/{print $2}'"),
            "gpu_power": sh("sensors | awk '/PPT/{print $2, $3}'"), "disk": sh("df -h /mnt/models | awk 'NR==2{print $3\"/\"$2}'")}


@app.get("/", response_class=HTMLResponse)
def index():
    return open(os.path.join(os.path.dirname(__file__), "web.html"), encoding="utf-8").read()


def main():
    cfg = C.load()
    if not cfg["web"].get("password"):
        cfg["web"]["password"] = secrets.token_urlsafe(12)
        C.save(cfg)
        print(f"[web] generated password (user 'sergio'): {cfg['web']['password']}", flush=True)
    import uvicorn
    uvicorn.run(app, host=cfg["web"]["host"], port=int(cfg["web"]["port"]), log_level="warning")


if __name__ == "__main__":
    main()
