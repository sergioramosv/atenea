"""Atenea web panel: login page, conversations, settings and status.  Run: python -m athena.web

Auth: a full-page login (password in config["web"]["password"], generated on first run). On success the
server sets a signed, HttpOnly session cookie valid for 30 days, so you log in once per device.

Conversations: each one is a JSON file in ~/.config/athena/chats/<id>.json holding
  {id, title, project, created, updated, log: [{q, a, events, meta, t}], messages: [agent messages]}
`log` is what the UI shows; `messages` is the model context, used to resume the conversation after a restart."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import threading
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from . import config as C
from .runtime import build_agent

app = FastAPI(title="Atenea", docs_url=None, redoc_url=None)
JOBS: dict[str, dict] = {}
AGENTS: dict[str, object] = {}          # live agents, one per conversation id
COOKIE = "atenea_session"
SESSION_DAYS = 30
HERE = os.path.dirname(__file__)
CHATS_DIR = os.path.join(os.path.dirname(C.PATH), "chats")
_lock = threading.Lock()


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


# ---------------- conversation storage ----------------
def _path(cid: str) -> str:
    if not cid or not cid.isalnum():
        raise HTTPException(400, "Bad conversation id")
    return os.path.join(CHATS_DIR, cid + ".json")


def _read(cid: str) -> dict:
    try:
        return json.load(open(_path(cid), encoding="utf-8"))
    except FileNotFoundError:
        raise HTTPException(404, "Conversation not found")


def _write(chat: dict):
    os.makedirs(CHATS_DIR, exist_ok=True)
    tmp = _path(chat["id"]) + ".tmp"
    json.dump(chat, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, _path(chat["id"]))


def _new_chat(project: str | None) -> dict:
    now = time.time()
    chat = {"id": uuid.uuid4().hex[:12], "title": "New chat", "project": project or "", "created": now,
            "updated": now, "log": [], "messages": []}
    _write(chat)
    return chat


def _running(cid: str) -> str | None:
    for jid, j in JOBS.items():
        if j["chat"] == cid and not j["done"]:
            return jid
    return None


def _title(text: str) -> str:
    t = " ".join(text.split())
    return (t[:60].rsplit(" ", 1)[0] + "…") if len(t) > 60 else (t or "New chat")


def _cwd(project: str | None) -> str:
    cfg = C.load()
    return os.path.expanduser(cfg.get("projects", {}).get(project or "", project or "") or cfg["workspace"])


def _agent(chat: dict):
    """The live agent for a conversation, rebuilt from disk when needed (after a restart, for example)."""
    ag = AGENTS.get(chat["id"])
    if ag is None:
        ag = build_agent(_cwd(chat.get("project")))
        if chat.get("messages"):
            ag.messages = [ag.messages[0]] + [m for m in chat["messages"] if m["role"] != "system"]
        AGENTS[chat["id"]] = ag
    return ag


# ---------------- running a turn ----------------
def _run_job(jid: str, cid: str, text: str):
    job = JOBS[jid]

    def ev(kind, data):
        if job["cancel"]:
            raise KeyboardInterrupt("cancelled")
        if kind in ("tool_call", "notice", "subagent"):
            job["events"].append({"t": round(time.time() - job["t0"], 1), "kind": kind,
                                  "data": {k: str(v)[:400] for k, v in data.items()}})

    def approve(tool, args):
        job["approval"] = {"tool": tool.name, "args": str(args)[:1500], "answer": None}
        for _ in range(600):
            if job["cancel"]:
                break
            if job["approval"]["answer"] is not None:
                ok = job["approval"]["answer"]
                job["approval"] = None
                return ok
            time.sleep(1)
        job["approval"] = None
        return False

    try:
        with _lock:
            chat = _read(cid)
        ag = _agent(chat)
        ag.on_event, ag.approve = ev, approve
        s0, t0 = ag.stats.requests, time.time()
        job["result"] = ag.run(text)
        job["meta"] = {"steps": ag.stats.requests - s0, "secs": round(time.time() - t0)}
    except KeyboardInterrupt:
        job["result"] = "_Stopped._"
        job["meta"] = {"steps": 0, "secs": round(time.time() - job["t0"]), "stopped": True}
    except Exception as e:
        job["result"] = f"**Error:** {type(e).__name__}: {e}"
    with _lock:
        try:
            chat = _read(cid)
            ag = AGENTS.get(cid)
            if ag is not None:
                chat["messages"] = ag.messages
            chat["log"].append({"q": text, "a": job["result"], "events": job["events"], "meta": job["meta"],
                                "t": job["t0"]})
            chat["updated"] = time.time()
            _write(chat)
        except HTTPException:                               # deleted while it was running
            pass
    job["done"] = True


class ChatIn(BaseModel):
    text: str
    chat: str | None = None
    project: str | None = None


class ChatPatch(BaseModel):
    title: str | None = None
    project: str | None = None


@app.get("/api/chats", dependencies=[Depends(auth)])
def list_chats(q: str = ""):
    out = []
    q = q.lower().strip()
    if os.path.isdir(CHATS_DIR):
        for f in os.listdir(CHATS_DIR):
            if not f.endswith(".json"):
                continue
            try:
                c = json.load(open(os.path.join(CHATS_DIR, f), encoding="utf-8"))
            except Exception:
                continue
            running = bool(_running(c["id"]))
            if not c["log"] and not running:
                continue                                    # empty chats are not listed
            if q and q not in c["title"].lower() and not any(
                    q in (e.get("q", "") + " " + (e.get("a") or "")).lower() for e in c["log"]):
                continue
            out.append({"id": c["id"], "title": c["title"], "project": c.get("project", ""),
                        "updated": c["updated"], "running": running, "turns": len(c["log"])})
    out.sort(key=lambda c: c["updated"], reverse=True)
    return {"chats": out}


@app.get("/api/chats/{cid}", dependencies=[Depends(auth)])
def get_chat(cid: str):
    c = _read(cid)
    return {"id": c["id"], "title": c["title"], "project": c.get("project", ""), "log": c["log"],
            "job": _running(cid)}


@app.patch("/api/chats/{cid}", dependencies=[Depends(auth)])
def patch_chat(cid: str, p: ChatPatch):
    with _lock:
        c = _read(cid)
        if p.title is not None and p.title.strip():
            c["title"] = p.title.strip()[:120]
        _write(c)
    return {"ok": True}


@app.delete("/api/chats/{cid}", dependencies=[Depends(auth)])
def delete_chat(cid: str):
    jid = _running(cid)
    if jid:
        JOBS[jid]["cancel"] = True
    AGENTS.pop(cid, None)
    with _lock:
        try:
            os.remove(_path(cid))
        except FileNotFoundError:
            pass
    return {"ok": True}


@app.post("/api/chat", dependencies=[Depends(auth)])
def chat(inp: ChatIn):
    if not inp.text.strip():
        raise HTTPException(400, "Empty message")
    with _lock:
        c = _read(inp.chat) if inp.chat else _new_chat(inp.project)
        if _running(c["id"]):
            raise HTTPException(409, "This conversation is still working")
        if not c["log"] and c["title"] == "New chat":
            c["title"] = _title(inp.text)
        c["updated"] = time.time()
        _write(c)
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"t0": time.time(), "events": [], "done": False, "result": None, "approval": None, "meta": None,
                 "chat": c["id"], "cancel": False}
    threading.Thread(target=_run_job, args=(jid, c["id"], inp.text), daemon=True).start()
    return {"job": jid, "chat": c["id"], "title": c["title"], "project": c.get("project", "")}


@app.get("/api/jobs/{jid}", dependencies=[Depends(auth)])
def job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    j = JOBS[jid]
    return {k: j[k] for k in ("events", "done", "result", "approval", "meta", "chat")} | \
        {"secs": round(time.time() - j["t0"])}


@app.post("/api/jobs/{jid}/approve/{answer}", dependencies=[Depends(auth)])
def approve(jid: str, answer: str):
    a = JOBS.get(jid, {}).get("approval")
    if a:
        a["answer"] = answer == "yes"
    return {"ok": bool(a)}


@app.post("/api/jobs/{jid}/stop", dependencies=[Depends(auth)])
def stop(jid: str):
    if jid in JOBS:
        JOBS[jid]["cancel"] = True
    return {"ok": jid in JOBS}


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
    for cid in list(AGENTS):                               # rebuild idle agents with the new settings
        if not _running(cid):
            AGENTS.pop(cid, None)
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
            "uptime": _sh("uptime -p"), "projects": list(cfg.get("projects", {}).keys()),
            "busy": sum(1 for j in JOBS.values() if not j["done"])}


# ---------------- pages & assets ----------------
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _valid(request.cookies.get(COOKIE)):
        return RedirectResponse("/")
    return open(os.path.join(HERE, "login.html"), encoding="utf-8").read()


@app.get("/", response_class=HTMLResponse)
@app.get("/c/{cid}", response_class=HTMLResponse)
def index(request: Request, cid: str | None = None):
    if not _valid(request.cookies.get(COOKIE)):
        return RedirectResponse("/login")
    return open(os.path.join(HERE, "web.html"), encoding="utf-8").read()


@app.get("/brand/{name}")
def brand(name: str):
    f = os.path.join(HERE, "brand", os.path.basename(name))
    if not os.path.isfile(f):
        raise HTTPException(404)
    return FileResponse(f, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
@app.get("/favicon.svg")
def favicon():
    return FileResponse(os.path.join(HERE, "brand", "atenea-icon.svg"), media_type="image/svg+xml")


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
