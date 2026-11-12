"""Atenea web panel: login page, chat, settings and status.  Run: python -m athena.web

Auth: a full-page login (password in config["web"]["password"], generated on first run). On success the
server sets a signed, HttpOnly session cookie valid for 30 days, so you log in once per device."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import subprocess
import threading
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from . import config as C
from .runtime import build_agent

app = FastAPI(title="Atenea", docs_url=None, redoc_url=None)
JOBS: dict[str, dict] = {}
AGENTS: dict[str, object] = {}          # one web conversation per project folder
COOKIE = "atenea_session"
SESSION_DAYS = 30
HERE = os.path.dirname(__file__)


# ---------------- auth ----------------
def _secret() -> bytes:
    cfg = C.load()
    if not cfg["web"].get("secret"):
        cfg["web"]["secret"] = secrets.token_hex(32)
        C.save(cfg)
    return cfg["web"]["secret"].encode()


def _make_token() -> str:
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{exp}.{sig}".encode()).decode()


def _valid(token: str | None) -> bool:
    if not token:
        return False
    try:
        exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split(".")
    except Exception:
        return False
    good = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and int(exp) > time.time()


def auth(request: Request):
    if not _valid(request.cookies.get(COOKIE)):
        raise HTTPException(401, "Not signed in")


class LoginIn(BaseModel):
    password: str


_attempts: dict[str, list[float]] = {}


@app.post("/api/login")
def login(inp: LoginIn, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    recent = [t for t in _attempts.get(ip, []) if time.time() - t < 300]
    if len(recent) >= 8:                                    # basic brute-force protection
        raise HTTPException(429, "Too many attempts, wait a few minutes")
    pw = C.load()["web"].get("password", "")
    if not pw or not secrets.compare_digest(inp.password, pw):
        _attempts[ip] = recent + [time.time()]
        time.sleep(0.8)
        raise HTTPException(401, "Wrong password")
    response.set_cookie(COOKIE, _make_token(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------------- chat ----------------
class ChatIn(BaseModel):
    text: str
    project: str | None = None
    new: bool = False


def _cwd(project: str | None) -> str:
    cfg = C.load()
    return os.path.expanduser(cfg.get("projects", {}).get(project or "", project or "") or cfg["workspace"])


def _run_job(jid: str, text: str, project: str | None, new: bool):
    job = JOBS[jid]
    cwd = _cwd(project)

    def ev(kind, data):
        if kind in ("tool_call", "notice", "subagent"):
            job["events"].append({"t": round(time.time() - job["t0"], 1), "kind": kind,
                                  "data": {k: str(v)[:400] for k, v in data.items()}})

    def approve(tool, args):
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
        s0, t0 = ag.stats.requests, time.time()
        job["result"] = ag.run(text)
        job["meta"] = {"steps": ag.stats.requests - s0, "secs": round(time.time() - t0)}
    except Exception as e:
        job["result"] = f"**Error:** {type(e).__name__}: {e}"
    job["done"] = True


@app.post("/api/chat", dependencies=[Depends(auth)])
def chat(inp: ChatIn):
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"t0": time.time(), "events": [], "done": False, "result": None, "approval": None, "meta": None}
    threading.Thread(target=_run_job, args=(jid, inp.text, inp.project, inp.new), daemon=True).start()
    return {"job": jid}


@app.get("/api/jobs/{jid}", dependencies=[Depends(auth)])
def job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    j = JOBS[jid]
    return {k: j[k] for k in ("events", "done", "result", "approval", "meta")} | {"secs": round(time.time() - j["t0"])}


@app.post("/api/jobs/{jid}/approve/{answer}", dependencies=[Depends(auth)])
def approve(jid: str, answer: str):
    a = JOBS.get(jid, {}).get("approval")
    if a:
        a["answer"] = answer == "yes"
    return {"ok": bool(a)}


@app.get("/api/history", dependencies=[Depends(auth)])
def history(project: str | None = None):
    ag = AGENTS.get(_cwd(project))
    if not ag:
        return {"messages": []}
    out = []
    for m in ag.messages[1:]:
        if m["role"] == "user" and not str(m.get("content", "")).startswith(("Your last tool call", "Your reply was",
                                                                              "Not finished yet")):
            out.append({"role": "user", "text": m["content"]})
        elif m["role"] == "assistant" and (m.get("content") or "").strip() and not m.get("tool_calls"):
            out.append({"role": "assistant", "text": m["content"]})
    return {"messages": out}


@app.post("/api/new", dependencies=[Depends(auth)])
def new_chat(project: str | None = None):
    AGENTS.pop(_cwd(project), None)
    return {"ok": True}


# ---------------- settings & status ----------------
@app.get("/api/config", dependencies=[Depends(auth)])
def get_config():
    cfg = C.load()
    if cfg["telegram"].get("token"):
        cfg["telegram"]["token"] = cfg["telegram"]["token"][:8] + "…(hidden)"
    cfg.pop("web", None)
    return cfg


@app.put("/api/config", dependencies=[Depends(auth)])
def put_config(new: dict):
    cfg = C.load()
    if "…(hidden)" in str(new.get("telegram", {}).get("token", "")):
        new.setdefault("telegram", {})["token"] = cfg["telegram"].get("token", "")
    new.pop("web", None)                                   # password/secret are not changed from the browser
    cfg.update(new)
    C.save(cfg)
    AGENTS.clear()
    return {"ok": True}


def _sh(c):
    return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()


@app.get("/api/status", dependencies=[Depends(auth)])
def status():
    cfg = C.load()
    return {"name": cfg.get("assistant_name", "Atenea"), "model": cfg["model"].get("model"),
            "ornith": _sh("systemctl is-active ornith"), "hermes": _sh("systemctl is-active athena-hermes"),
            "ram_used": _sh("free -m | awk '/Mem/{print $3}'"), "ram_total": _sh("free -m | awk '/Mem/{print $2}'"),
            "gpu_temp": _sh("sensors | awk '/edge/{gsub(/[+°C]/,\"\",$2); print $2}'"),
            "gpu_power": _sh("sensors | awk '/PPT/{print $2}'"),
            "disk_used": _sh("df -BG /mnt/models | awk 'NR==2{print $3}'"),
            "disk_total": _sh("df -BG /mnt/models | awk 'NR==2{print $2}'"),
            "uptime": _sh("uptime -p"), "projects": list(cfg.get("projects", {}).keys())}


# ---------------- pages ----------------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _valid(request.cookies.get(COOKIE)):
        return RedirectResponse("/")
    return open(os.path.join(HERE, "login.html"), encoding="utf-8").read()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _valid(request.cookies.get(COOKIE)):
        return RedirectResponse("/login")
    return open(os.path.join(HERE, "web.html"), encoding="utf-8").read()


def main():
    cfg = C.load()
    if not cfg["web"].get("password"):
        cfg["web"]["password"] = secrets.token_urlsafe(12)
        C.save(cfg)
        print(f"[web] generated password: {cfg['web']['password']}", flush=True)
    _secret()
    import uvicorn
    uvicorn.run(app, host=cfg["web"]["host"], port=int(cfg["web"]["port"]), log_level="warning")


if __name__ == "__main__":
    main()
