"""Atenea web panel: login page, conversations, settings and status.  Run: python -m atenea.web

Auth: a full-page login (password in config["web"]["password"], generated on first run). On success the
server sets a signed, HttpOnly session cookie valid for 30 days, so you log in once per device.

Conversations: each one is a JSON file in ~/.config/atenea/chats/<id>.json holding
  {id, title, project, created, updated, log: [{q, a, events, meta, t}], messages: [agent messages]}
`log` is what the UI shows; `messages` is the model context, used to resume the conversation after a restart."""
from __future__ import annotations

import base64
import difflib
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
import uuid

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel

from . import artifacts as A
from . import background as BG
from . import docs as DOCS
from . import knowledge as KB
from . import media as MEDIA
from . import monitors as MON
from . import notify as NOTIFY
from . import scheduler as SCH
from . import config as C
from . import memory as MEM
from .runtime import build_agent

app = FastAPI(title="Atenea", docs_url=None, redoc_url=None)
JOBS: dict[str, dict] = {}
AGENTS: dict[str, object] = {}          # live agents, one per conversation id
QUEUES: dict[str, list[dict]] = {}      # messages sent while a conversation is busy: run in order afterwards
BGAGENTS: dict[str, dict] = {}          # subagents running in the background
# Runners: another machine (e.g. the Mac running `atenea`) executes the file/shell tools of a conversation, while
# the agent and the model stay here. The runner long-polls for tool calls and posts the results back.
RUNNERS: dict[str, dict] = {}           # runner id -> {name, platform, cwd, seen, calls: Queue, results: {}}
RUNNER_TOOLS = ("bash", "read_file", "write_file", "edit_file", "list_files", "grep")
ACTIVITY_INSTR = """- BACKGROUND WORK: you can keep the user unblocked. bg_start runs commands in the background;
  monitor_start watches a command, process or log and wakes you up in this chat when something happens
  ("tell me when the build ends", "watch the log for errors", "check every 5 minutes if the site is up");
  delegate(..., background=true) runs a subagent in the background and its result arrives later as a message.
  Messages that start with [Monitor …] or [Background subagent …] come from those, not from the user: act on them
  and tell the user briefly what happened.
- ATTACHMENTS: lines like [attachment: /path/file (type, size)] are files the user uploaded. For images use
  look_at_image (it really sees them); for video extract frames with ffmpeg and look at them; read text, PDFs
  (pdftotext) or spreadsheets with tools. Show results with ![…](/path).
- ASKING THE USER: when you need a decision or a missing detail, call ask_user with a short question and 2-4 clear
  options (the user can also type their own answer). Don't use it for trivial things you can decide yourself.
- ABOUT THIS PANEL (so you can explain it): the user talks to you in the Atenea web panel. Permission modes (pill above
  the message box or Shift+Tab): "Ask first" = a popup with Allow / Deny before every command or file edit; "Accept
  edits" = edits freely, asks before commands; "Auto" = only asks before external actions; "Plan" = read-only, you
  propose a plan with Approve buttons. Approvals show a diff and an "Always allow" button. Each user message has
  Rewind (undo your file edits / cut the chat) and Fork. Slash commands: /help /context /mcp /skills /tools /agents
  /hooks /tasks /doctor /compact /memory /remember /init /review /schedule /usage. The tray above the box shows
  background shells, subagents and monitors. If the user wants you to ask before acting, tell them to pick "Ask first"."""
COOKIE = "atenea_session"
SESSION_DAYS = 30
HERE = os.path.dirname(__file__)
CHATS_DIR = os.path.join(os.path.dirname(C.PATH), "chats")
CKPT_DIR = os.path.join(os.path.dirname(C.PATH), "checkpoints")
TRASH_DIR = os.path.join(os.path.dirname(C.PATH), "trash")
EDIT_TOOLS = ("write_file", "edit_file")
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


def cli_token() -> str:
    """Token for the terminal CLI on this machine (stored in the config, readable only by the user)."""
    cfg = C.load()
    if not cfg["web"].get("cli_token"):
        cfg["web"]["cli_token"] = secrets.token_hex(24)
        C.save(cfg)
    return cfg["web"]["cli_token"]


def auth(request: Request):
    if _valid(request.cookies.get(COOKIE)):
        return
    h = request.headers.get("authorization", "")
    if h.startswith("Bearer ") and secrets.compare_digest(h[7:].strip(), cli_token()):
        return
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
    secure = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https" \
        or request.headers.get("host", "").endswith(".ts.net")
    response.set_cookie(COOKIE, _make_token(), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure)
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


MODES = ("ask", "edits", "auto", "plan")


def _mode(m: str | None) -> str:
    return m if m in MODES else (C.load().get("default_mode") or "auto")


# ---------------- quick replies ----------------
# Greetings, thanks and short factual questions are answered without reasoning (~1-2 s instead of ~5 s).
# Conservative: anything that looks like a task, mentions files/code or is long keeps full reasoning.
_TASK_WORDS = re.compile(r"\b(arregl|crea|crear|haz|hazme|genera|escrib|busca|investig|analiz|compar|programa|codig|código|"
                         r"archivo|fichero|carpeta|repo|test|error|bug|instala|configura|ejecuta|corre|lanza|revisa|resume|"
                         r"resumen|traduce|calcula|planifica|expli|por qu|correo|email|calendario|agenda|imagen|foto|"
                         r"fix|create|make|write|search|find|build|run|install|explain|why|code|file|image|plan|review)",
                         re.I)


def _is_quick(text: str, mode: str) -> bool:
    if not C.load().get("quick_replies", True) or mode == "plan":
        return False
    t = text.strip()
    return (len(t) <= 140 and "\n" not in t and "@" not in t and "[attachment:" not in t and "/" not in t
            and not _TASK_WORDS.search(t))


# ---------------- prompt-cache friendly context ----------------
# The system prompt never changes during a chat (no date, memory frozen at the chat's start), so llama.cpp can reuse
# the processed prefix. Things that change go at the END of each new message instead.
def _turn_note(ag) -> str:
    parts = [time.strftime("Now: %a %d %b %Y %H:%M")]
    if ag.mode != "auto":
        parts.append(MODE_TEXT.get(ag.mode, ag.mode))
    now, snap = MEM.load(), getattr(ag, "_mem_snapshot", None)
    if snap is not None:
        added = [f for f in now if f not in snap]
        gone = [f for f in snap if f not in now]
        if added or gone:
            parts.append("Memory updated since this chat started: " + "; ".join(
                [f"+ {f}" for f in added[-20:]] + [f"- (forgotten) {f}" for f in gone[-20:]]))
    return "\n\n[" + " · ".join(parts) + "]"


# Each chat is pinned to one llama.cpp slot while it works, and its KV cache is saved to disk after every turn and
# restored before the next one: coming back to a long chat costs ~0.1 s instead of re-reading it (minutes).
SLOT_DIR = "/mnt/models/llamacpp/slots"          # must match ornith's --slot-save-path
SLOT_CAP_BYTES = 40 * 1024**3
_slots = {"n": None, "busy": set(), "owner": {}, "used": {}}
_slot_lock = threading.Lock()


def _llama(path: str, body: dict | None = None, timeout: int = 120) -> dict:
    import urllib.request
    base = C.load()["model"]["url"].split("/v1/")[0]
    key = open(os.path.expanduser("~/.config/ornith/api_key")).read().strip() \
        if os.path.exists(os.path.expanduser("~/.config/ornith/api_key")) else ""
    req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _slot_file(cid: str) -> str:
    return f"atenea-{cid}.bin"


def _slot_acquire(cid: str, ag) -> int | None:
    try:
        with _slot_lock:
            if _slots["n"] is None:
                _slots["n"] = int(_llama("/props").get("total_slots") or 1)
            free = [i for i in range(_slots["n"]) if i not in _slots["busy"]]
            if not free:
                return None
            mine = [i for i in free if _slots["owner"].get(i) == cid]
            slot = mine[0] if mine else min(free, key=lambda i: (_slots["owner"].get(i) is not None, _slots["used"].get(i, 0)))
            _slots["busy"].add(slot)
            restore = True                               # cheap (~0.1 s) and safe even if a subagent used the slot
            _slots["owner"][slot] = cid
        if restore and not os.path.exists(os.path.join(SLOT_DIR, _slot_file(cid))):
            base = _base_file(ag)                        # new chat: start from the processed instructions
            if os.path.exists(os.path.join(SLOT_DIR, base)):
                try:
                    r = _llama(f"/slots/{slot}?action=restore", {"filename": base})
                    print(f"[slots] new chat {cid}: base restored ({r.get('n_restored')} tokens)", flush=True)
                except Exception:
                    try:
                        os.remove(os.path.join(SLOT_DIR, base))
                    except OSError:
                        pass
        if restore and os.path.exists(os.path.join(SLOT_DIR, _slot_file(cid))):
            try:
                r = _llama(f"/slots/{slot}?action=restore", {"filename": _slot_file(cid)})
                print(f"[slots] restored {r.get('n_restored')} tokens of {cid} into slot {slot}", flush=True)
            except Exception as e:                   # e.g. saved by an older server config: drop it, re-read once
                print(f"[slots] could not restore {cid} ({e}); discarding that cache file", flush=True)
                try:
                    os.remove(os.path.join(SLOT_DIR, _slot_file(cid)))
                except OSError:
                    pass
        ag.profile.extra = {"id_slot": slot}
        return slot
    except Exception as e:
        print(f"[slots] acquire failed ({e}); running unpinned", flush=True)
        ag.profile.extra = None
        return None


def _base_file(ag) -> str:
    import hashlib
    h = hashlib.sha1((ag.messages[0]["content"] + json.dumps(ag.schemas(), sort_keys=True)).encode()).hexdigest()[:12]
    return f"atenea-base-{h}.bin"


def _build_base(slot: int, ag, name: str):
    """Process only the instructions + tools once and keep that state, so new chats start without re-reading them."""
    body = {"messages": [{"role": "system", "content": ag.messages[0]["content"]}, {"role": "user", "content": "hola"}],
            "tools": ag.schemas(), "max_tokens": 1, "id_slot": slot, "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False}}
    try:
        _llama("/v1/chat/completions", body, timeout=600)
        _llama(f"/slots/{slot}?action=save", {"filename": name})
        print(f"[slots] base ready: {name}", flush=True)
    except Exception as e:
        print(f"[slots] base build failed: {e}", flush=True)


def _slot_release(cid: str, ag, slot: int | None):
    ag.profile.extra = None
    if slot is None:
        return
    keep_busy = False
    try:
        if ag.stats.requests:
            _llama(f"/slots/{slot}?action=save", {"filename": _slot_file(cid)})
            _slot_gc()
            base = _base_file(ag)
            if not os.path.exists(os.path.join(SLOT_DIR, base)):
                keep_busy = True                         # build it in the background on this slot, then free it

                def bg():
                    try:
                        _build_base(slot, ag, base)
                    finally:
                        with _slot_lock:
                            _slots["busy"].discard(slot)
                            _slots["owner"][slot] = None
                            _slots["used"][slot] = time.time()
                threading.Thread(target=bg, daemon=True).start()
    except Exception as e:
        print(f"[slots] save failed: {e}", flush=True)
    finally:
        if not keep_busy:
            with _slot_lock:
                _slots["busy"].discard(slot)
                _slots["used"][slot] = time.time()


def _slot_gc():
    """Keep saved KV caches under SLOT_CAP_BYTES, dropping the least recently used."""
    try:
        files = [os.path.join(SLOT_DIR, f) for f in os.listdir(SLOT_DIR) if f.startswith("atenea-")]
        files.sort(key=os.path.getmtime, reverse=True)
        total = 0
        for f in files:
            total += os.path.getsize(f)
            if total > SLOT_CAP_BYTES:
                os.remove(f)
    except OSError:
        pass


MODE_TEXT = {"ask": "Ask first: the user gets an Allow/Deny popup before every command and file edit",
             "edits": "Accept edits: file edits run freely, commands show an Allow/Deny popup",
             "auto": "Auto: you act freely; only external actions (send, delete, publish…) show a popup",
             "plan": "Plan: read-only; propose a plan, the user approves it with buttons"}


def _mode_note(mode: str) -> str:
    return f"\n# Current permission mode of this chat: {MODE_TEXT.get(mode, mode)}.\n"


def _ctx_max() -> int:
    return int(C.load()["model"].get("context") or 204800)


def _new_chat(project: str | None, mode: str | None = None) -> dict:
    now = time.time()
    chat = {"id": uuid.uuid4().hex[:12], "title": "New chat", "project": project or "", "mode": _mode(mode),
            "created": now, "updated": now, "log": [], "messages": [], "usage": None}
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
    ws = os.path.expanduser(cfg["workspace"])
    p = os.path.expanduser(cfg.get("projects", {}).get(project or "", project or "") or ws)
    return p if os.path.isabs(p) else os.path.join(ws, p)


def _agent(chat: dict):
    """The live agent for a conversation, rebuilt from disk when needed (after a restart, for example)."""
    ag = AGENTS.get(chat["id"])
    if ag is None:
        snap = chat.get("mem_snapshot")
        if snap is None:                                 # memory as it was when the chat started: keeps the prefix stable
            snap = MEM.load()
            with _lock:
                try:
                    c2 = _read(chat["id"])
                    c2["mem_snapshot"] = snap
                    _write(c2)
                except HTTPException:
                    pass
        ag = build_agent(_cwd(chat.get("project")), mem_facts=snap)
        ag._mem_snapshot = list(snap)
        if chat.get("messages"):
            ag.messages = [ag.messages[0]] + [m for m in chat["messages"] if m["role"] != "system"]
        # keep the context meter right before the first model call of this process (it reports 0 until then)
        ag.context_tokens = (chat.get("usage") or {}).get("ctx") or _est(ag.messages) + _est(ag.schemas())
        ag.messages[0]["content"] += "\n" + A.INSTRUCTIONS + "\n" + ACTIVITY_INSTR + "\n" + MEDIA.INSTRUCTIONS + "\n" + DOCS.INSTRUCTIONS + "\n" + KB.INSTRUCTIONS
        for t in BG.tools(lambda: ag.cwd, chat["id"]) + MON.tools(lambda: ag.cwd, chat["id"]):
            ag._tools[t.name] = t
        ag.spawn_bg = lambda sub, task, name, _cid=chat["id"]: _spawn_bg(_cid, sub, task, name)
        def ask_user(question: str, options: list | None = None) -> str:
            opts = [str(o)[:120] for o in (options or []) if str(o).strip()][:6]
            fn = getattr(ag, "_ask", None)
            if not fn:
                return "Cannot ask right now; continue with your best judgement and mention the assumption."
            a = fn(question[:500], opts)
            return f"The user answered: {a}" if a is not None else "The user did not answer in 30 minutes; continue with your best judgement and say what you assumed."
        from .registry import Tool as _Tool

        def _image_bytes(path: str, _cid=chat["id"]) -> bytes | str:
            p = path if os.path.isabs(os.path.expanduser(path)) else os.path.join(ag.cwd, path)
            p = os.path.expanduser(p)
            if os.path.isfile(p):                         # on ZEUS (uploads, generated images, local chats)
                return open(p, "rb").read()
            if chat.get("runner"):                        # on the user's computer
                raw = _runner_call(_cid, "read_b64", {"path": path}, timeout=120)
                if raw.startswith("Error") or raw.startswith("\0missing"):
                    return f"Error: cannot read {path} on {chat.get('runner_name') or 'the computer'}"
                import base64 as _b64
                return _b64.b64decode(raw)
            return f"Error: image not found: {path}"

        def look_at_image(path: str, question: str = "") -> str:
            data = _image_bytes(path)
            return data if isinstance(data, str) else MEDIA.look_at_image(data, question)

        def edit_image(path: str, instruction: str) -> str:
            data = _image_bytes(path)
            if isinstance(data, str):
                return data
            tmp = os.path.join(UPLOADS_DIR, "edit-src-" + uuid.uuid4().hex[:6] + ".png")
            os.makedirs(UPLOADS_DIR, exist_ok=True)
            open(tmp, "wb").write(data)
            return MEDIA.edit_image(tmp, instruction)

        for _t in (
            _Tool("look_at_image", "See an image with a vision model: describe it, read its text, or answer a question "
                  "about it. `path`: file path (uploads, generated images, or a file in the project).",
                  {"type": "object", "properties": {"path": {"type": "string"}, "question": {"type": "string"}},
                   "required": ["path"]}, look_at_image),
            _Tool("generate_image", "Create an image from a detailed English prompt (~40 s). Returns a markdown line to "
                  "show it. Optional width/height (256-1536, default 1024) and seed.",
                  {"type": "object", "properties": {"prompt": {"type": "string"}, "width": {"type": "integer"},
                   "height": {"type": "integer"}, "seed": {"type": "integer"}}, "required": ["prompt"]},
                  lambda prompt, width=1024, height=1024, seed=-1: MEDIA.generate_image(prompt, width, height, seed)),
            _Tool("search_documents", "Search the user's documents (their folders, uploads, files you created) and all "
                  "past conversations, by meaning and words. Returns the most relevant passages with their source.",
                  {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
                   "required": ["query"]}, lambda query, k=6: KB.search_tool(query, k)),
            _Tool("create_document", "Create a real PDF, Word (docx) or Excel (xlsx) file from Markdown (tables become "
                  "Excel sheets). Returns a markdown link to show it.",
                  {"type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"},
                   "format": {"type": "string", "enum": ["pdf", "docx", "xlsx"]}}, "required": ["title", "content"]},
                  lambda title, content, format="pdf": DOCS.create_document(title, content, format)),
            _Tool("edit_image", "Edit an existing image following an instruction (~60 s): change style, objects, "
                  "background, text, season… Returns a markdown line to show the result.",
                  {"type": "object", "properties": {"path": {"type": "string"}, "instruction": {"type": "string"}},
                   "required": ["path", "instruction"]}, edit_image)):
            ag._tools[_t.name] = _t
        ag._tools["ask_user"] = _Tool("ask_user", "Ask the user a question and wait for the answer. Give 2-4 short "
                                      "`options` they can click (they can also type something else).",
                                      {"type": "object", "properties": {"question": {"type": "string"},
                                       "options": {"type": "array", "items": {"type": "string"}}},
                                       "required": ["question"]}, ask_user)
        if chat.get("runner"):                           # file & shell tools run on the user's computer
            ag.cwd = chat.get("project") or ag.cwd
            for name in RUNNER_TOOLS:
                if name in ag._tools:
                    ag._tools[name].fn = (lambda n, _cid=chat["id"]: (lambda **kw: _runner_call(_cid, n, kw)))(name)
            for name in ("bg_start", "bg_logs", "bg_stop", "bg_list", "monitor_start", "monitor_list", "monitor_stop"):
                ag._tools.pop(name, None)
            where = f"{chat.get('runner_name') or 'the user’s computer'} ({chat.get('runner_platform') or 'remote'})"
            ag.messages[0]["content"] += (f"\n- IMPORTANT: in this conversation your file and shell tools (bash, read_file, "
                f"write_file, edit_file, list_files, grep) run on {where}, in the folder {ag.cwd} — NOT on ZEUS. "
                "Use that platform's commands (e.g. macOS: brew, BSD sed/grep). web_search, memory and the rest still run "
                "on ZEUS. Background shells and monitors are not available here.")
        ag._tools["create_artifact"] = A.make_tool(lambda: ag.cwd, chat["id"],
                                                   on_create=lambda m: getattr(ag, "_on_artifact", lambda _: None)(m))
        AGENTS[chat["id"]] = ag
    ag.mode = chat.get("mode") or _mode(None)
    ag.stream = True
    return ag


# ---------------- running a turn ----------------
RESUME_NOTE = ("[The server restarted while you were working on my last message, so your work was interrupted. "
               "Continue from where you left off: check what was already done (files, commands, messages sent) "
               "before repeating any action, then finish the task.]\n\nMy message was:\n")


def _save_progress(cid: str, ag, job: dict, force: bool = False):
    """Write the conversation to disk while a task runs (at most every few seconds), so a restart loses nothing."""
    if not force and time.time() - job.get("_saved", 0) < 4:
        return
    job["_saved"] = time.time()
    with _lock:
        try:
            c = _read(cid)
        except HTTPException:
            return
        c["messages"] = ag.messages
        if c.get("pending"):
            c["pending"]["events"] = job["events"][-200:]
            c["pending"]["artifacts"] = job["artifacts"]
        _write(c)


def _save_queue(cid: str):
    """Mirror the in-memory queue in the conversation file (caller holds _lock)."""
    try:
        c = _read(cid)
    except HTTPException:
        return
    c["queue"] = QUEUES.get(cid, [])
    _write(c)


def _run_job(jid: str, cid: str, text: str, resume: bool = False, origin: str | None = None, label: str = ""):
    job = JOBS[jid]
    holder: dict = {}

    def ev(kind, data):
        if job["cancel"]:
            raise KeyboardInterrupt("cancelled")
        if kind == "delta":
            lv = job["live"]
            key = "thinking" if data["kind"] == "reasoning" else "text"
            lv[key] = (lv[key] + data["text"])[-8000:]
            return
        if kind == "turn":
            job["live"] = {"thinking": "", "text": ""}
        if kind == "todos":
            job["todos"] = data["todos"]
        if kind in ("tool_call", "notice", "subagent"):
            job["events"].append({"t": round(time.time() - job["t0"], 1), "kind": kind,
                                  "data": {k: str(v)[:400] for k, v in data.items()}})
        if kind in ("turn", "tool_call", "subagent") and "ag" in holder:
            _save_progress(cid, holder["ag"], job)

    def approve(tool, args):
        remote = bool(holder.get("remote"))
        if _allowed_by_rule(tool.name, args):
            job["events"].append({"t": round(time.time() - job["t0"], 1), "kind": "notice",
                                  "data": {"text": f"{tool.name} allowed by an 'always allow' rule"}})
            return True
        ag = holder.get("ag")
        _notify_chat(cid, f"⏸ Atenea needs your OK to run {tool.name}")
        job["approval"] = {"tool": tool.name, "args": str(args)[:1500], "answer": None,
                           "diff": _diff_for(tool.name, args, ag.cwd if ag else "/", cid if remote else None),
                           **_rule_for(tool.name, args)}
        for _ in range(600):
            if job["cancel"]:
                break
            if job["approval"]["answer"] is not None:
                ok = job["approval"]["answer"]
                if ok == "always":
                    _add_rule(job["approval"], cid)
                job["approval"] = None
                return bool(ok)
            time.sleep(1)
        job["approval"] = None
        return False

    try:
        with _lock:
            chat = _read(cid)
        ag = _agent(chat)
        holder["ag"] = ag
        ag.on_event, ag.approve = ev, approve

        remote = bool(chat.get("runner"))
        holder["remote"] = remote

        def before(t, a):
            if remote and t.name in EDIT_TOOLS and a.get("path"):
                p = os.path.join(ag.cwd, a["path"]) if not a["path"].startswith(("/", "~")) else a["path"]
                if p not in job["ckpt"]:
                    raw = _runner_call(cid, "read_raw", {"path": p}, timeout=60)
                    job["ckpt"][p] = None if raw.startswith("\0missing") else ("\0skip" if raw.startswith("Error") else raw)
                return
            if t.name in EDIT_TOOLS and a.get("path"):
                p = os.path.realpath(os.path.join(ag.cwd, os.path.expanduser(a["path"])))
                if p not in job["ckpt"]:
                    try:
                        job["ckpt"][p] = open(p, encoding="utf-8").read() if os.path.getsize(p) < 5_000_000 else "\0skip"
                    except FileNotFoundError:
                        job["ckpt"][p] = None
                    except (OSError, UnicodeDecodeError):
                        job["ckpt"][p] = "\0skip"
        ag.before_tool = before
        job["msg_start"] = len(ag.messages)
        def ask(question, options):
            job["question"] = {"question": question, "options": options, "answer": None}
            _notify_chat(cid, f"❓ {question}")
            for _ in range(1800):
                if job["cancel"]:
                    raise KeyboardInterrupt("cancelled")
                if job["question"]["answer"] is not None:
                    a = job["question"]["answer"]
                    job["question"] = None
                    return a
                time.sleep(1)
            job["question"] = None
            return None
        ag._ask = ask
        ag._on_artifact = lambda m: job["artifacts"].append({"id": m["id"], "title": m["title"], "version": m["version"]})
        s0, t0 = ag.stats.requests, time.time()
        job["mode"] = ag.mode
        job["base"] = (ag.stats.completion_tokens, ag.stats.secs_model)
        text_for_model = text if chat.get("runner") else _expand_mentions(text, ag.cwd)   # the CLI expands @files
        ag.note = _turn_note(ag)
        fast = _is_quick(text, ag.mode) and not resume
        ag.profile.think = False if fast else None
        job["quick"] = fast
        slot = _slot_acquire(cid, ag)
        try:
            job["result"] = ag.run(RESUME_NOTE + text_for_model if resume else text_for_model)
        finally:
            _slot_release(cid, ag, slot)
        job["meta"] = {"steps": ag.stats.requests - s0, "secs": round(time.time() - t0), "mode": job.get("mode"),
                       **({"quick": True} if job.get("quick") else {})}
        ag.profile.think = None
    except KeyboardInterrupt:
        job["result"] = "_Stopped._"
        job["meta"] = {"steps": 0, "secs": round(time.time() - job["t0"]), "stopped": True}
    except Exception as e:
        job["result"] = f"**Error:** {type(e).__name__}: {e}"
    nxt = None
    with _lock:
        try:
            chat = _read(cid)
            ag = AGENTS.get(cid)
            if ag is not None:
                chat["messages"] = ag.messages
                chat["usage"] = _usage(ag, job)
            idx = len(chat["log"])
            files = [p for p, v in job["ckpt"].items() if v != "\0skip"]
            if files:
                os.makedirs(os.path.join(CKPT_DIR, cid), exist_ok=True)
                json.dump({p: job["ckpt"][p] for p in files},
                          open(os.path.join(CKPT_DIR, cid, f"{idx}.json"), "w", encoding="utf-8"), ensure_ascii=False)
            chat["log"].append({"q": text, "a": job["result"], "events": job["events"], "meta": job["meta"],
                                "files": files, "msg_start": job.get("msg_start"),
                                "t": job["t0"], "artifacts": job["artifacts"], "todos": job["todos"],
                                **({"origin": origin, "label": label} if origin else {}),
                                **({"resumed": True} if resume else {})})
            chat["pending"] = None
            chat["updated"] = time.time()
            _write(chat)
        except HTTPException:                               # deleted while it was running
            QUEUES.pop(cid, None)
        q = QUEUES.get(cid)
        if q and not job["cancel"]:
            item = q.pop(0)
            if not q:
                QUEUES.pop(cid, None)
            _save_queue(cid)
            nxt = _start(cid, item["text"], origin=item.get("origin"), label=item.get("label", ""))
        elif not q:
            QUEUES.pop(cid, None)
    job["next"] = nxt
    job["done"] = True
    secs = time.time() - job["t0"]
    if job.get("scheduled") or (secs > 20 and not job["cancel"]):
        _notify_chat(cid, ("⏰ " if job.get("scheduled") else "✅ ") + str(job["result"] or "")[:700])


def _start(cid: str, text: str, resume: bool = False, t0: float | None = None, origin: str | None = None,
           label: str = "") -> str:
    """Start a task for a conversation. Caller holds _lock. The message is saved as `pending` right away."""
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"t0": t0 or time.time(), "events": [], "done": False, "result": None, "approval": None, "meta": None,
                 "chat": cid, "cancel": False, "text": text, "next": None, "artifacts": [], "resumed": resume,
                 "live": {"thinking": "", "text": ""}, "todos": [], "ckpt": {}, "origin": origin, "label": label,
                 "question": None}
    try:
        c = _read(cid)
        old = c.get("pending") or {}
        c["pending"] = {"q": text, "t": JOBS[jid]["t0"], "origin": origin, "label": label, "events": old.get("events", []) if resume else [],
                        "artifacts": old.get("artifacts", []) if resume else []}
        if resume:
            JOBS[jid]["events"] = list(c["pending"]["events"])
            JOBS[jid]["artifacts"] = list(c["pending"]["artifacts"])
            JOBS[jid]["events"].append({"t": round(time.time() - JOBS[jid]["t0"], 1), "kind": "notice",
                                        "data": {"text": "server restarted · resuming where it left off"}})
        c["updated"] = time.time()
        _write(c)
    except HTTPException:
        pass
    threading.Thread(target=_run_job, args=(jid, cid, text, resume, origin, label), daemon=True).start()
    return jid


def _inject(cid: str | None, text: str, origin: str, label: str):
    """A message from a monitor or a background subagent: wakes Atenea up in that conversation."""
    if not cid:
        return
    with _lock:
        try:
            _read(cid)
        except HTTPException:
            return
        if _running(cid):
            QUEUES.setdefault(cid, []).append({"id": uuid.uuid4().hex[:8], "text": text, "origin": origin, "label": label})
            _save_queue(cid)
        else:
            _start(cid, text, origin=origin, label=label)


def _spawn_bg(cid: str, sub, task: str, name: str) -> str:
    sid = uuid.uuid4().hex[:6]
    rec = {"id": sid, "name": name, "task": task[:500], "chat": cid, "status": "running", "events": [],
           "started": time.time(), "result": None, "cancel": False}
    BGAGENTS[sid] = rec

    def ev(kind, data):
        if rec["cancel"]:
            raise KeyboardInterrupt("cancelled")
        if kind in ("tool_call", "notice"):
            rec["events"].append({"t": round(time.time() - rec["started"], 1), "kind": kind,
                                  "data": {k: str(v)[:200] for k, v in data.items()}})
            rec["events"] = rec["events"][-60:]

    def run():
        sub.on_event = ev
        sub.approve = lambda t, a: False                  # background work cannot stop to ask: actions needing OK are skipped
        try:
            rec["result"] = sub.run(task)
            rec["status"] = "done"
        except KeyboardInterrupt:
            rec["result"], rec["status"] = "Stopped by the user.", "stopped"
        except Exception as e:
            rec["result"], rec["status"] = f"Error: {e}", "error"
        rec["ended"] = time.time()
        if rec["status"] != "stopped":
            _inject(cid, f"[Background subagent «{name}» finished ({round(rec['ended'] - rec['started'])} s)]\n"
                         f"Task: {task[:600]}\n\nResult:\n{rec['result']}", "subagent", name)
    threading.Thread(target=run, daemon=True).start()
    return sid


def _monitor_fired(m: dict, text: str):
    _inject(m.get("chat"), f"[Monitor «{m['name']}» fired]\n{text}\n\nAct on this if needed and tell the user briefly "
                           "what happened.", "monitor", m["name"])
    if m.get("chat"):
        _notify_chat(m["chat"], f"🔔 Monitor «{m['name']}» fired")


MON.on_fire = _monitor_fired


def resume_interrupted():
    """On startup: resume tasks that were running when the server stopped, and restore queued messages."""
    if not os.path.isdir(CHATS_DIR):
        return
    with _lock:
        for f in os.listdir(CHATS_DIR):
            if not f.endswith(".json"):
                continue
            cid = f[:-5]
            try:
                c = _read(cid)
            except Exception:
                continue
            if c.get("queue"):
                QUEUES[cid] = c["queue"]
            p = c.get("pending")
            if p and p.get("q"):
                print(f"[web] resuming interrupted task in {cid}: {p['q'][:60]!r}", flush=True)
                _start(cid, p["q"], resume=True, t0=p.get("t"), origin=p.get("origin"), label=p.get("label") or "")


def _usage(ag, job: dict | None = None) -> dict:
    """Context in use and generation speed of the last turn (what the bar above the composer shows)."""
    out, secs = ag.stats.completion_tokens, ag.stats.secs_model
    if job and job.get("base"):
        out, secs = out - job["base"][0], secs - job["base"][1]
    tm = getattr(ag, "timings", {}) or {}
    tps = tm.get("predicted_per_second") or (out / secs if secs > 0.5 else None)
    return {"ctx": ag.context_tokens, "max": _ctx_max(), "out": out, "tps": round(tps, 1) if tps else None,
            "read_tps": round(tm["prompt_per_second"], 0) if tm.get("prompt_per_second") else None,
            "read_tokens": tm.get("prompt_n"), "calls": ag.stats.requests}


class ChatIn(BaseModel):
    text: str
    chat: str | None = None
    project: str | None = None
    mode: str | None = None
    client: str | None = None                # "cli" when the chat was started from the terminal


class ChatPatch(BaseModel):
    title: str | None = None
    mode: str | None = None


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
            if not c["log"] and not running and not c.get("pending"):
                continue                                    # empty chats are not listed
            if q and q not in c["title"].lower() and not any(
                    q in (e.get("q", "") + " " + (e.get("a") or "")).lower() for e in c["log"]):
                continue
            out.append({"id": c["id"], "title": c["title"], "project": c.get("project", ""), "client": c.get("client"),
                        "updated": c["updated"], "running": running, "turns": len(c["log"])})
    out.sort(key=lambda c: c["updated"], reverse=True)
    return {"chats": out}


class NewChatIn(BaseModel):
    project: str | None = None
    mode: str | None = None
    client: str | None = None


@app.post("/api/chats", dependencies=[Depends(auth)])
def create_chat(n: NewChatIn):
    """Create an empty conversation (the terminal CLI does this at start so it can show the app link)."""
    with _lock:
        c = _new_chat(n.project, n.mode)
        if n.client:
            c["client"] = n.client
            _write(c)
    return {"id": c["id"], "url": f"{_panel_url()}/c/{c['id']}"}


@app.get("/api/chats/{cid}", dependencies=[Depends(auth)])
def get_chat(cid: str):
    c = _read(cid)
    jid = _running(cid)
    return {"id": c["id"], "title": c["title"], "project": c.get("project", ""), "log": c["log"], "client": c.get("client"),
            "job": jid, "pending": JOBS[jid]["text"] if jid else (c.get("pending") or {}).get("q"),
            "pending_origin": (c.get("pending") or {}).get("origin"), "pending_label": (c.get("pending") or {}).get("label"), "queue": QUEUES.get(cid, []), "mode": c.get("mode") or _mode(None),
            "usage": c.get("usage") or {"ctx": 0, "max": _ctx_max(), "tps": None, "out": 0}}


@app.patch("/api/chats/{cid}", dependencies=[Depends(auth)])
def patch_chat(cid: str, p: ChatPatch):
    with _lock:
        c = _read(cid)
        if p.title is not None and p.title.strip():
            c["title"] = p.title.strip()[:120]
        if p.mode is not None:
            c["mode"] = _mode(p.mode)
            if cid in AGENTS:
                AGENTS[cid].mode = c["mode"]                 # takes effect on the next tool call, even mid-task
        _write(c)
    return {"ok": True}


@app.delete("/api/chats/{cid}", dependencies=[Depends(auth)])
def delete_chat(cid: str):
    jid = _running(cid)
    QUEUES.pop(cid, None)
    if jid:
        JOBS[jid]["cancel"] = True
    AGENTS.pop(cid, None)
    try:
        os.remove(os.path.join(SLOT_DIR, _slot_file(cid)))
    except OSError:
        pass
    with _lock:
        try:                                              # to the trash for 30 days, not gone forever
            os.makedirs(TRASH_DIR, exist_ok=True)
            c = _read(cid)
            c["deleted"] = time.time()
            json.dump(c, open(os.path.join(TRASH_DIR, cid + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
            os.remove(_path(cid))
        except (FileNotFoundError, HTTPException):
            pass
    return {"ok": True}


@app.get("/api/trash", dependencies=[Depends(auth)])
def trash_list():
    out = []
    if os.path.isdir(TRASH_DIR):
        for f in os.listdir(TRASH_DIR):
            p = os.path.join(TRASH_DIR, f)
            try:
                c = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            if time.time() - c.get("deleted", 0) > 30 * 86400:
                os.remove(p)
                continue
            out.append({"id": c["id"], "title": c["title"], "deleted": c.get("deleted"), "turns": len(c.get("log", []))})
    return {"items": sorted(out, key=lambda x: -(x["deleted"] or 0))}


@app.post("/api/trash/{cid}/restore", dependencies=[Depends(auth)])
def trash_restore(cid: str):
    p = os.path.join(TRASH_DIR, os.path.basename(cid) + ".json")
    if not os.path.isfile(p):
        raise HTTPException(404, "Not in the trash")
    c = json.load(open(p, encoding="utf-8"))
    c.pop("deleted", None)
    with _lock:
        _write(c)
    os.remove(p)
    return {"ok": True}


@app.get("/api/backups", dependencies=[Depends(auth)])
def backups():
    from . import backup as BK
    return {"dir": BK.DEST, "items": BK.listing()[:10]}


@app.post("/api/backups", dependencies=[Depends(auth)])
def backup_now():
    from . import backup as BK
    return BK.make()


@app.post("/api/chat", dependencies=[Depends(auth)])
def chat(inp: ChatIn):
    if not inp.text.strip():
        raise HTTPException(400, "Empty message")
    with _lock:
        c = _read(inp.chat) if inp.chat else _new_chat(inp.project, inp.mode)
        if inp.client and not c.get("client"):
            c["client"] = inp.client
        running = _running(c["id"])
        if running:                                        # busy: queue it, it runs when the current task ends
            item = {"id": uuid.uuid4().hex[:8], "text": inp.text}
            QUEUES.setdefault(c["id"], []).append(item)
            _save_queue(c["id"])
            return {"queued": item["id"], "job": running, "chat": c["id"], "title": c["title"],
                    "project": c.get("project", ""), "mode": c.get("mode"), "position": len(QUEUES[c["id"]])}
        if not c["log"] and c["title"] == "New chat":
            c["title"] = _title(inp.text)
        c["updated"] = time.time()
        _write(c)
        jid = _start(c["id"], inp.text)
    return {"job": jid, "chat": c["id"], "title": c["title"], "project": c.get("project", ""), "mode": c["mode"]}


@app.get("/api/jobs/{jid}", dependencies=[Depends(auth)])
def job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404)
    j = JOBS[jid]
    ag = AGENTS.get(j["chat"])
    return {k: j.get(k) for k in ("events", "done", "result", "approval", "meta", "chat", "next", "artifacts", "live", "todos", "question")} | \
        {"secs": round(time.time() - j["t0"]), "usage": _usage(ag, j) if ag is not None else None}


@app.post("/api/jobs/{jid}/approve/{answer}", dependencies=[Depends(auth)])
def approve(jid: str, answer: str):
    a = JOBS.get(jid, {}).get("approval")
    if a:
        a["answer"] = "always" if answer == "always" else answer == "yes"
    return {"ok": bool(a)}


class AnswerIn(BaseModel):
    answer: str


@app.post("/api/jobs/{jid}/answer", dependencies=[Depends(auth)])
def answer(jid: str, a: AnswerIn):
    q = JOBS.get(jid, {}).get("question")
    if q:
        q["answer"] = a.answer.strip()[:2000] or "(empty answer)"
    return {"ok": bool(q)}


@app.post("/api/jobs/{jid}/stop", dependencies=[Depends(auth)])
def stop(jid: str):
    """Stop the running task. Queued messages are not run: they are returned so the UI can put them back."""
    if jid not in JOBS:
        return {"ok": False, "queued": []}
    JOBS[jid]["cancel"] = True
    with _lock:
        q = QUEUES.pop(JOBS[jid]["chat"], [])
        _save_queue(JOBS[jid]["chat"])
    return {"ok": True, "queued": [i["text"] for i in q]}


@app.delete("/api/chats/{cid}/queue/{qid}", dependencies=[Depends(auth)])
def unqueue(cid: str, qid: str):
    with _lock:
        q = QUEUES.get(cid, [])
        QUEUES[cid] = [i for i in q if i["id"] != qid]
        if not QUEUES[cid]:
            QUEUES.pop(cid, None)
        _save_queue(cid)
    return {"ok": True}


# ---------------- permissions: diffs and "always allow" rules ----------------
def _diff_for(name: str, args: dict, cwd: str, remote_chat: str | None = None) -> str | None:
    if name not in EDIT_TOOLS or not args.get("path"):
        return None
    if remote_chat:
        raw = _runner_call(remote_chat, "read_raw", {"path": args["path"]}, timeout=60)
        cur = "" if raw.startswith(("\0missing", "Error")) else raw
    else:
        p = os.path.join(cwd, os.path.expanduser(args["path"]))
        try:
            cur = open(p, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            cur = ""
    new = args.get("content", "") if name == "write_file" else cur.replace(args.get("old", ""), args.get("new", ""), 1)
    lines = list(difflib.unified_diff(cur.splitlines(), new.splitlines(), fromfile=args["path"], tofile=args["path"],
                                      lineterm="", n=2))
    return "\n".join(lines[:400]) + ("\n… (diff truncated)" if len(lines) > 400 else "") if lines else "(no changes)"


def _bash_prefix(cmd: str) -> str:
    words = cmd.strip().split()
    if not words:
        return ""
    return " ".join(words[:2]) if len(words) > 1 and not words[1].startswith("-") else words[0]


def _rule_for(name: str, args: dict) -> dict:
    if name in EDIT_TOOLS:
        return {"always": "Allow all edits in this chat (switches to Accept edits)"}
    if name == "bash":
        pre = _bash_prefix(str(args.get("command", "")))
        return {"always": f"Always allow commands starting with “{pre}”", "rule": {"tool": "bash", "prefix": pre}} if pre else {}
    return {"always": f"Always allow {name}", "rule": {"tool": name}}


def _allowed_by_rule(name: str, args: dict) -> bool:
    for r in C.load().get("allow_rules") or []:
        if r.get("tool") != name:
            continue
        if name != "bash":
            return True
        cmd = str(args.get("command", "")).strip()
        pre = r.get("prefix", "")
        if pre and (cmd == pre or cmd.startswith(pre + " ")) and not any(x in cmd for x in (";", "&&", "||", "|", "`", "$(")):
            return True
    return False


def _add_rule(approval: dict, cid: str):
    if approval["tool"] in EDIT_TOOLS:
        with _lock:
            try:
                c = _read(cid)
                c["mode"] = "edits"
                _write(c)
            except HTTPException:
                pass
        if cid in AGENTS:
            AGENTS[cid].mode = "edits"
        return
    rule = approval.get("rule")
    if rule:
        cfg = C.load()
        rules = cfg.setdefault("allow_rules", [])
        if rule not in rules:
            rules.append(rule)
            C.save(cfg)


# ---------------- rewind (checkpoints) ----------------
class RewindIn(BaseModel):
    index: int
    what: str = "both"              # both | code | conversation


@app.post("/api/chats/{cid}/rewind", dependencies=[Depends(auth)])
def rewind(cid: str, r: RewindIn):
    """Go back to before message `index`: restore the files Atenea edited since then and/or cut the conversation."""
    if _running(cid):
        raise HTTPException(409, "Wait until Atenea finishes (or stop it) before rewinding")
    with _lock:
        c = _read(cid)
        if not 0 <= r.index < len(c["log"]):
            raise HTTPException(400, "No such message")
        restored = []
        if r.what in ("both", "code"):
            for i in range(len(c["log"]) - 1, r.index - 1, -1):
                f = os.path.join(CKPT_DIR, cid, f"{i}.json")
                if not os.path.isfile(f):
                    continue
                for path, content in json.load(open(f, encoding="utf-8")).items():
                    if c.get("runner"):
                        out = _runner_call(cid, "restore_raw", {"path": path, "content": content}, timeout=60)
                        if not out.startswith("Error"):
                            restored.append(path)
                        continue
                    try:
                        if content is None:
                            if os.path.exists(path):
                                os.remove(path)
                        else:
                            os.makedirs(os.path.dirname(path), exist_ok=True)
                            open(path, "w", encoding="utf-8").write(content)
                        restored.append(path)
                    except OSError:
                        pass
        q = c["log"][r.index]["q"]
        if r.what in ("both", "conversation"):
            start = c["log"][r.index].get("msg_start")
            if start is None:
                raise HTTPException(400, "This message is older than checkpoints; only code can be restored")
            c["messages"] = c["messages"][:start]
            c["log"] = c["log"][:r.index]
            for i in range(r.index, 10000):
                f = os.path.join(CKPT_DIR, cid, f"{i}.json")
                if not os.path.exists(f):
                    break
                os.remove(f)
            c["usage"] = None
            AGENTS.pop(cid, None)
        c["updated"] = time.time()
        _write(c)
    return {"ok": True, "restored": sorted(set(restored)), "q": q}


@app.post("/api/chats/{cid}/fork", dependencies=[Depends(auth)])
def fork(cid: str, upto: int | None = None):
    """Duplicate a conversation (optionally only up to message `upto`, exclusive) into a new one."""
    with _lock:
        c = _read(cid)
        n = dict(c)
        n["id"] = uuid.uuid4().hex[:12]
        n["title"] = (c["title"] + " (copy)")[:120]
        if upto is not None and 0 <= upto < len(c["log"]) and c["log"][upto].get("msg_start") is not None:
            n["messages"] = c["messages"][:c["log"][upto]["msg_start"]]
            n["log"] = c["log"][:upto]
        n["pending"], n["queue"] = None, []
        n["created"] = n["updated"] = time.time()
        _write(n)
    return {"id": n["id"]}


@app.get("/api/chats/{cid}/export", dependencies=[Depends(auth)])
def export(cid: str, fmt: str = "md"):
    c = _read(cid)
    if fmt == "json":
        return Response(json.dumps(c, ensure_ascii=False, indent=1), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="atenea-{cid}.json"'})
    out = [f"# {c['title']}\n"]
    for e in c["log"]:
        out.append(f"## You\n\n{e['q']}\n\n## Atenea\n\n{e.get('a') or ''}\n")
    safe = re.sub(r"[^\w\- ]+", "", c["title"])[:60].strip() or "chat"
    return Response("\n".join(out), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.md"'})


# ---------------- memory ----------------
class MemoryIn(BaseModel):
    facts: list[str]


@app.get("/api/memory", dependencies=[Depends(auth)])
def get_memory():
    return {"facts": MEM.load()}


@app.put("/api/memory", dependencies=[Depends(auth)])
def put_memory(m: MemoryIn):
    MEM.save([" ".join(f.split())[:500] for f in m.facts if f.strip()])
    for cid in list(AGENTS):                               # new facts reach idle conversations on their next message
        if not _running(cid):
            AGENTS.pop(cid, None)
    return {"ok": True}


# ---------------- @files, commands, compact ----------------
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".cache", "target"}


def _expand_mentions(text: str, cwd: str) -> str:
    """`@path` in a message attaches that file (like Claude Code). The log keeps the original text."""
    extra = []
    for m in re.finditer(r"(?<![\w@])@([\w./~-][^\s,;:!?()]*)", text):
        rel = m.group(1).rstrip(".")
        p = os.path.realpath(os.path.join(cwd, os.path.expanduser(rel)))
        if os.path.isfile(p) and os.path.getsize(p) < 150_000:
            try:
                extra.append(f'<file path="{rel}">\n{open(p, encoding="utf-8").read()}\n</file>')
            except (OSError, UnicodeDecodeError):
                pass
        elif os.path.isdir(p):
            try:
                extra.append(f'<directory path="{rel}">\n' + "\n".join(sorted(os.listdir(p))[:300]) + "\n</directory>")
            except OSError:
                pass
    return text + ("\n\n" + "\n\n".join(extra[:10]) if extra else "")


@app.get("/api/files", dependencies=[Depends(auth)])
def files(q: str = "", chat: str | None = None, project: str | None = None):
    cwd = _cwd(_read(chat).get("project") if chat else project)
    q = q.lower()
    out = []
    for root, dirs, fs in os.walk(cwd):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        rel_root = os.path.relpath(root, cwd)
        for name in [*dirs, *fs]:
            rel = name if rel_root == "." else os.path.join(rel_root, name)
            if not q or q in rel.lower():
                out.append(rel + ("/" if name in dirs else ""))
        if len(out) > 400 or len(out) + 1 > 20000:
            break
    out.sort(key=lambda r: (len(r), r))
    return {"cwd": cwd, "files": out[:50]}


def _command_dirs(cwd: str) -> list[str]:
    return [os.path.join(os.path.dirname(C.PATH), "commands"), os.path.join(cwd, ".atenea", "commands")]


@app.get("/api/commands", dependencies=[Depends(auth)])
def commands(chat: str | None = None):
    """Custom slash commands: markdown files in ~/.config/atenea/commands or <project>/.atenea/commands.
    $ARGUMENTS in the file is replaced by whatever follows the command."""
    cwd = _cwd(_read(chat).get("project") if chat else None)
    out = {}
    for d in _command_dirs(cwd):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".md"):
                    body = open(os.path.join(d, f), encoding="utf-8").read()
                    first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
                    out[f[:-3]] = {"name": f[:-3], "description": first[:100], "body": body}
    return {"commands": list(out.values())}


@app.post("/api/chats/{cid}/compact", dependencies=[Depends(auth)])
def compact(cid: str):
    """Summarise the conversation and continue from the summary (frees context, like /compact)."""
    if _running(cid):
        raise HTTPException(409, "Wait until Atenea finishes")
    c = _read(cid)
    convo = "\n\n".join(f"USER: {e['q']}\nATENEA: {e.get('a') or ''}" for e in c["log"])[-60000:]
    if not convo.strip():
        return {"ok": True, "summary": ""}
    from .llm import LLM
    ag = _agent(c)
    r = LLM(ag.profile).chat([{"role": "system", "content": "You write compact, faithful summaries of conversations."},
                              {"role": "user", "content": "Summarise this conversation so we can continue it later: "
                               "goals, decisions, files and commands involved, current state and pending work. "
                               "Be specific and brief (max ~400 words), same language as the user.\n\n" + convo}])
    summary = (r["message"].get("content") or "").strip()
    with _lock:
        c = _read(cid)
        c["messages"] = [{"role": "user", "content": "Summary of our conversation so far (it was compacted):\n" + summary},
                         {"role": "assistant", "content": "Understood, I have the context. Let's continue."}]
        for e in c["log"]:
            e["msg_start"] = None
        c["usage"] = None
        c["compacted"] = time.time()
        _write(c)
    AGENTS.pop(cid, None)
    return {"ok": True, "summary": summary}


# ---------------- introspection: /context /mcp /skills /tools /agents /hooks /doctor ----------------
def _est(x) -> int:
    return max(0, round(len(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)) / 3.6))


@app.get("/api/context", dependencies=[Depends(auth)])
def context(chat: str | None = None, project: str | None = None):
    """What fills the context window, by category (like /context in Claude Code)."""
    if chat:
        c = _read(chat)
        ag = _agent(c)
        msgs, real = c.get("messages") or [], (c.get("usage") or {}).get("ctx")
    else:
        ag, msgs, real = build_agent(_cwd(project)), [], None
    sysmsg = ag.messages[0]["content"]
    mem, proj = MEM.block(), MEM.project_block(ag.cwd)
    cats = [["System instructions", _est(sysmsg) - _est(mem) - _est(proj)], ["Tools", _est(ag.schemas())],
            ["Memory", _est(mem)], ["Project (ATENEA.md)", _est(proj)]]
    by = {"user": 0, "assistant": 0, "tool": 0, "reasoning": 0}
    for m in msgs:
        if m.get("role") == "system":
            continue
        by[m["role"] if m["role"] in by else "user"] += _est(m.get("content") or "") + (_est(m["tool_calls"]) if m.get("tool_calls") else 0)
        by["reasoning"] += _est(m.get("reasoning_content") or "")
    cats += [["Your messages", by["user"]], ["Atenea's answers", by["assistant"]], ["Tool results", by["tool"]],
             ["Reasoning", by["reasoning"]]]
    est_total = sum(v for _, v in cats)
    if real and est_total:                                   # scale estimates to the real count from the model
        k = real / est_total
        cats = [[n, round(v * k)] for n, v in cats]
    total = sum(v for _, v in cats)
    return {"categories": [{"name": n, "tokens": v} for n, v in cats], "total": total, "max": _ctx_max(),
            "exact": bool(real), "messages": len([m for m in msgs if m.get("role") != "system"])}


@app.get("/api/mcp", dependencies=[Depends(auth)])
def mcp(reconnect: str | None = None):
    from .runtime import mcp_status
    return {"servers": mcp_status(C.load(), reconnect)}


@app.get("/api/skills", dependencies=[Depends(auth)])
def skills():
    from .skills import SkillSet
    cfg = C.load()
    ss = SkillSet(*cfg.get("skills_dirs", []))
    return {"dirs": cfg.get("skills_dirs", []), "skills": [{"name": n, "description": v["description"], "path": v["path"]}
                                                           for n, v in ss.skills.items()]}


@app.get("/api/tools", dependencies=[Depends(auth)])
def tools_list(chat: str | None = None):
    ag = _agent(_read(chat)) if chat else build_agent(_cwd(None))
    out = []
    for t in ag._tools.values():
        need = ag._needs_approval(t)
        out.append({"name": t.name, "description": (t.description or "")[:200],
                    "approval": "blocked in plan" if need == "block" else "asks" if need else "free"})
    out.append({"name": "delegate", "description": "Hand a subtask to a subagent", "approval": "free"})
    return {"mode": ag.mode, "tools": out}


@app.get("/api/agents", dependencies=[Depends(auth)])
def agents_list(chat: str | None = None):
    from . import subagents as SUB
    cwd = _cwd(_read(chat).get("project")) if chat else _cwd(None)
    return {"agents": [{k: v for k, v in a.items() if k != "prompt"} for a in SUB.load(cwd).values()]}


@app.get("/api/hooks", dependencies=[Depends(auth)])
def hooks_list():
    return {"hooks": C.load().get("hooks") or {}}


@app.get("/api/doctor", dependencies=[Depends(auth)])
def doctor():
    import urllib.request
    checks = []
    def add(name, ok, detail):
        checks.append({"name": name, "ok": ok, "detail": detail})
    try:
        t0 = time.time()
        urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5).read()
        add("Model server (ornith)", True, f"healthy · {round((time.time() - t0) * 1000)} ms")
    except Exception as e:
        add("Model server (ornith)", False, str(e)[:120])
    from .runtime import mcp_status
    for m in mcp_status(C.load()):
        add(f"MCP · {m['name']}", not m["error"] and m["enabled"], m["error"] or (f"{len(m['tools'])} tools" if m["enabled"] else "disabled"))
    try:
        from .web_tools import _searx
        add("Atenea buscador (SearXNG)", bool(_searx("test", 1)), "private meta-search on :8888" if _searx("test", 1) else "down: using DuckDuckGo as fallback")
    except Exception as e:
        add("Atenea buscador (SearXNG)", False, str(e)[:120])
    try:
        from .web_tools import web_search
        r = web_search("test", 1)
        add("Web search", "Error" not in r[:20] and "No results" not in r, "DuckDuckGo reachable" if "1." in r else r[:100])
    except Exception as e:
        add("Web search", False, str(e)[:120])
    tg = C.load().get("telegram") or {}
    add("Atenea Telegram", bool(tg.get("token") and tg.get("allowed_users")),
        "configured" if tg.get("token") else "not configured: add the bot token in Settings")
    st = os.statvfs(os.path.expanduser("~"))
    free = st.f_bavail * st.f_frsize / 1e9
    add("Disk space", free > 10, f"{free:.0f} GB free in home")
    add("Memory", True, f"{len(MEM.load())} facts remembered")
    add("Conversations", True, f"{len([f for f in os.listdir(CHATS_DIR) if f.endswith('.json')]) if os.path.isdir(CHATS_DIR) else 0} saved")
    return {"checks": checks}


# ---------------- activity tray: shells, subagents, monitors ----------------
@app.get("/api/activity", dependencies=[Depends(auth)])
def activity(chat: str | None = None):
    shells = [p for p in BG.listing() if p["status"] == "running" or time.time() - p["started"] < 600]
    agents = [{k: v for k, v in a.items()} for a in BGAGENTS.values()
              if a["status"] == "running" or time.time() - a.get("ended", time.time()) < 600]
    mons = [{k: v for k, v in m.items() if k != "hits"} | {"last_hit": m["hits"][-1] if m["hits"] else None}
            for m in MON.listing() if m["status"] == "watching" or time.time() - m["created"] < 600]
    return {"shells": shells, "agents": agents, "monitors": mons, "job": _running(chat) if chat else None}


@app.post("/api/bgagents/{sid}/stop", dependencies=[Depends(auth)])
def bgagent_stop(sid: str):
    if sid in BGAGENTS:
        BGAGENTS[sid]["cancel"] = True
    return {"ok": sid in BGAGENTS}


@app.post("/api/monitors/{mid}/stop", dependencies=[Depends(auth)])
def monitor_stop(mid: str):
    return {"text": MON.stop(mid)}


# ---------------- Atenea documentos (knowledge base) ----------------
class KbIn(BaseModel):
    folders: list[str]


@app.get("/api/knowledge", dependencies=[Depends(auth)])
def kb_stats():
    st = KB.stats()
    st["configured"] = (C.load().get("knowledge") or {}).get("folders", ["~/Documentos"])
    return st


@app.put("/api/knowledge", dependencies=[Depends(auth)])
def kb_folders(k: KbIn):
    cfg = C.load()
    cfg.setdefault("knowledge", {})["folders"] = [f.strip() for f in k.folders if f.strip()]
    C.save(cfg)
    KB.reindex()
    return {"ok": True}


@app.post("/api/knowledge/reindex", dependencies=[Depends(auth)])
def kb_reindex():
    KB.reindex()
    return {"ok": True}


def _kb_loop():
    time.sleep(60)
    while True:
        try:
            KB.reindex(wait=True)                        # incremental: only new or changed files and chats
        except Exception as e:
            print(f"[knowledge] {e}", flush=True)
        time.sleep(900)


# ---------------- background processes ----------------
@app.get("/api/bg", dependencies=[Depends(auth)])
def bg_list():
    return {"procs": BG.listing()}


@app.get("/api/bg/{pid}/logs", dependencies=[Depends(auth)])
def bg_logs(pid: str, lines: int = 200):
    return {"text": BG.tail(pid, lines)}


@app.post("/api/bg/{pid}/stop", dependencies=[Depends(auth)])
def bg_stop(pid: str):
    return {"text": BG.stop(pid)}


# ---------------- scheduled tasks & notifications ----------------
def _panel_url() -> str:
    cfg = C.load()
    if cfg["web"].get("public_url"):
        return cfg["web"]["public_url"].rstrip("/")
    ip = subprocess.run("hostname -I", shell=True, capture_output=True, text=True).stdout.split()
    return f"http://{ip[0] if ip else 'zeus'}:{cfg['web']['port']}"


def _notify_chat(cid: str, text: str):
    try:
        title = _read(cid)["title"]
    except Exception:
        title = "Atenea"
    NOTIFY.send(text, f"{_panel_url()}/c/{cid}", title=title)


def run_schedule(item: dict) -> str:
    with _lock:
        if item.get("chat"):
            try:
                c = _read(item["chat"])
            except HTTPException:
                c = _new_chat(None, item.get("mode"))
        else:
            c = _new_chat(None, item.get("mode"))
            c["title"] = f"⏰ {item['name']} · {time.strftime('%d %b %H:%M')}"
            _write(c)
        if _running(c["id"]):
            QUEUES.setdefault(c["id"], []).append({"id": uuid.uuid4().hex[:8], "text": item["prompt"]})
            _save_queue(c["id"])
            return c["id"]
        jid = _start(c["id"], item["prompt"])
        JOBS[jid]["scheduled"] = True
    SCH.update(item["id"], last_chat=c["id"])
    return c["id"]


def _scheduler_loop():
    while True:
        try:
            for item in SCH.due(time.time()):
                print(f"[web] running scheduled task {item['name']!r}", flush=True)
                run_schedule(item)
        except Exception as e:
            print(f"[web] scheduler error: {e}", flush=True)
        time.sleep(20)


class ScheduleIn(BaseModel):
    name: str | None = None
    prompt: str | None = None
    when: str | None = None
    mode: str | None = None
    enabled: bool | None = None


BRIEFING_PROMPT = """Prepara mi resumen de la mañana, en español, breve y fácil de leer en el móvil (máx. ~200 palabras):
1. Saludo con la fecha de hoy (día de la semana) y si es festivo en España.
2. El tiempo de hoy donde estoy: usa web_fetch con https://wttr.in/?format=%l:+%c+%t+(máx+%h)+lluvia+%p&lang=es
   (o mi ciudad si la tienes en memoria) y di si conviene paraguas o abrigo.
3. Si tienes herramientas de calendario o correo: mis reuniones de hoy y los correos importantes sin leer. Si no las
   tienes, sáltalo sin comentarlo.
4. Lo que tengo pendiente: busca con search_documents en mis conversaciones recientes tareas que quedaron a medias o
   cosas que dije que haría, y revisa list_scheduled.
5. 2-3 noticias de hoy sobre los temas que me interesan según tu memoria (por defecto: IA y modelos locales), con enlace.
Termina con una frase corta de ánimo. Sin preámbulos. No guardes nada en memoria."""


@app.post("/api/schedules/briefing", dependencies=[Depends(auth)])
def schedule_briefing(s_: ScheduleIn):
    when = s_.when or "weekdays 08:00"
    for it in SCH.load():                                 # only one briefing
        if it.get("preset") == "briefing":
            SCH.remove(it["id"])
    it = SCH.add("☀️ Resumen de la mañana", BRIEFING_PROMPT, when, "auto")
    SCH.update(it["id"], preset="briefing")
    return it


@app.get("/api/schedules", dependencies=[Depends(auth)])
def schedules():
    return {"items": SCH.load()}


@app.post("/api/schedules", dependencies=[Depends(auth)])
def schedule_add(s_: ScheduleIn):
    try:
        return SCH.add(s_.name or "Task", s_.prompt or "", s_.when or "", _mode(s_.mode))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.patch("/api/schedules/{sid}", dependencies=[Depends(auth)])
def schedule_patch(sid: str, s_: ScheduleIn):
    try:
        it = SCH.update(sid, **s_.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not it:
        raise HTTPException(404)
    return it


@app.delete("/api/schedules/{sid}", dependencies=[Depends(auth)])
def schedule_del(sid: str):
    return {"ok": SCH.remove(sid)}


@app.post("/api/schedules/{sid}/run", dependencies=[Depends(auth)])
def schedule_run(sid: str):
    it = next((i for i in SCH.load() if i["id"] == sid), None)
    if not it:
        raise HTTPException(404)
    return {"chat": run_schedule(it)}


# ---------------- runners (tools executed on another machine) ----------------
import queue as _queue


class RunnerIn(BaseModel):
    name: str = "computer"
    platform: str = ""
    cwd: str
    chat: str | None = None


class RunnerResult(BaseModel):
    id: str
    output: str


def _runner_online(rid: str | None) -> bool:
    r = RUNNERS.get(rid or "")
    return bool(r) and time.time() - r["seen"] < 60


def _runner_call(cid: str, tool: str, args: dict, timeout: int = 1800) -> str:
    try:
        rid = _read(cid).get("runner")
    except HTTPException:
        rid = None
    if not _runner_online(rid):
        return ("Error: the computer that runs this conversation's files and commands is offline. "
                "Tell the user to open `atenea` again in that folder (atenea -c).")
    r, call_id = RUNNERS[rid], uuid.uuid4().hex[:10]
    r["results"][call_id] = None
    r["calls"].put({"id": call_id, "tool": tool, "args": args})
    t0 = time.time()
    while time.time() - t0 < timeout:
        if r["results"].get(call_id) is not None:
            return r["results"].pop(call_id)
        if not _runner_online(rid):
            break
        time.sleep(0.1)
    r["results"].pop(call_id, None)
    return f"Error: no answer from {r['name']} (it went offline or the command took too long)."


@app.post("/api/runners", dependencies=[Depends(auth)])
def runner_register(ri: RunnerIn):
    rid = uuid.uuid4().hex[:10]
    RUNNERS[rid] = {"id": rid, "name": ri.name[:60], "platform": ri.platform[:40], "cwd": ri.cwd, "seen": time.time(),
                    "calls": _queue.Queue(), "results": {}}
    with _lock:
        if ri.chat:
            c = _read(ri.chat)
        else:
            c = _new_chat(ri.cwd, "edits")
        c.update(runner=rid, runner_name=ri.name[:60], runner_platform=ri.platform[:40], project=ri.cwd, client="cli")
        _write(c)
    AGENTS.pop(c["id"], None)                             # rebuild with remote tools
    return {"runner": rid, "chat": c["id"], "url": f"{_panel_url()}/c/{c['id']}"}


@app.get("/api/runners/{rid}/next", dependencies=[Depends(auth)])
def runner_next(rid: str, wait: int = 25):
    r = RUNNERS.get(rid)
    if not r:
        raise HTTPException(404, "Unknown runner (the server restarted): register again")
    r["seen"] = time.time()
    try:
        call = r["calls"].get(timeout=max(1, min(wait, 50)))
    except _queue.Empty:
        return {}
    r["seen"] = time.time()
    return {"call": call}


@app.post("/api/runners/{rid}/result", dependencies=[Depends(auth)])
def runner_result(rid: str, res: RunnerResult):
    r = RUNNERS.get(rid)
    if not r:
        raise HTTPException(404, "Unknown runner")
    r["seen"] = time.time()
    if res.id in r["results"]:
        r["results"][res.id] = res.output
    return {"ok": True}


# ---------------- installer for other computers (Mac / Linux) ----------------
INSTALL_SH = r"""#!/bin/bash
# Atenea Code for this computer: `curl -fsSL __SERVER__/install.sh | bash`
set -e
SERVER="${ATENEA_SERVER:-__SERVER__}"
PY=""
for p in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then PY="$(command -v $p)"; break; fi
done
if [ -z "$PY" ]; then echo "Atenea needs Python 3.10 or newer. On a Mac: brew install python   (then run this again)"; exit 1; fi
echo "→ Installing Atenea Code with $($PY --version) into ~/.atenea"
mkdir -p "$HOME/.atenea" "$HOME/.local/bin"
[ -x "$HOME/.atenea/venv/bin/python" ] || "$PY" -m venv "$HOME/.atenea/venv"
"$HOME/.atenea/venv/bin/pip" install -q --upgrade pip prompt_toolkit qrcode >/dev/null
rm -rf "$HOME/.atenea/src.new" && mkdir -p "$HOME/.atenea/src.new"
curl -fsSL "$SERVER/dist/atenea.tar.gz" | tar xz -C "$HOME/.atenea/src.new"
rm -rf "$HOME/.atenea/src" && mv "$HOME/.atenea/src.new" "$HOME/.atenea/src"
cat > "$HOME/.local/bin/atenea" <<EOS
#!/bin/bash
PYTHONPATH="\$HOME/.atenea/src" exec "\$HOME/.atenea/venv/bin/python" -m atenea.cli "\$@"
EOS
chmod +x "$HOME/.local/bin/atenea"
echo "✓ Installed: ~/.local/bin/atenea"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *)
  SHELLRC="$HOME/.zshrc"; [ -n "$BASH_VERSION" ] && [ ! -f "$HOME/.zshrc" ] && SHELLRC="$HOME/.bashrc"
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELLRC"; echo "  (added ~/.local/bin to PATH in $SHELLRC — open a new terminal)";;
esac
if [ -z "$ATENEA_SKIP_LOGIN" ]; then "$HOME/.local/bin/atenea" login "$SERVER" < /dev/tty; fi
"""


@app.get("/install.sh")
def install_sh():
    return Response(INSTALL_SH.replace("__SERVER__", _panel_url()), media_type="text/x-shellscript")


@app.get("/dist/atenea.tar.gz")
def dist():
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(HERE, arcname="atenea", filter=lambda ti: None if "__pycache__" in ti.name else ti)
    return Response(buf.getvalue(), media_type="application/gzip")


# ---------------- push notifications ----------------
class PushSub(BaseModel):
    subscription: dict
    label: str = ""


@app.get("/sw.js")
def service_worker():
    return FileResponse(os.path.join(HERE, "sw.js"), media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/api/push/key", dependencies=[Depends(auth)])
def push_key():
    return {"key": NOTIFY.vapid()["public"], "devices": [{"label": x["label"], "added": x["added"]} for x in NOTIFY.subs()]}


@app.post("/api/push/subscribe", dependencies=[Depends(auth)])
def push_subscribe(p: PushSub):
    return {"devices": NOTIFY.subscribe(p.subscription, p.label)}


@app.post("/api/push/unsubscribe", dependencies=[Depends(auth)])
def push_unsubscribe(p: PushSub):
    NOTIFY.unsubscribe(p.subscription.get("endpoint", ""))
    return {"ok": True}


@app.post("/api/push/test", dependencies=[Depends(auth)])
def push_test():
    n = NOTIFY.webpush("Atenea", "Notifications are working ✓", "/", force=True)
    return {"sent": n}


@app.post("/api/presence", dependencies=[Depends(auth)])
def presence():
    NOTIFY.LAST_SEEN["t"] = time.time()                  # a tab is visible: no need to push
    return {"ok": True}


# ---------------- voice (atenea-voz service on :8083) ----------------
VOICE = os.environ.get("ATENEA_VOICE_URL", "http://127.0.0.1:8083")


@app.post("/api/stt", dependencies=[Depends(auth)])
async def stt(request: Request):
    import urllib.request
    data = await request.body()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "Recording too long")
    try:
        req = urllib.request.Request(VOICE + "/stt", data, {"Content-Type": "application/octet-stream"})
        return json.load(urllib.request.urlopen(req, timeout=180))
    except Exception as e:
        raise HTTPException(503, f"Voice service unavailable: {e}")


class TtsIn(BaseModel):
    text: str
    voice: str | None = None


@app.post("/api/tts", dependencies=[Depends(auth)])
def tts(t: TtsIn):
    import urllib.request
    cfg = C.load().get("voice") or {}
    body = {"text": t.text[:3000], "voice": t.voice or cfg.get("voice", "es_ES-sharvard-medium"), "speaker": cfg.get("speaker", 1)}
    try:
        req = urllib.request.Request(VOICE + "/tts", json.dumps(body).encode(), {"Content-Type": "application/json"})
        wav = urllib.request.urlopen(req, timeout=120).read()
    except Exception as e:
        raise HTTPException(503, f"Voice service unavailable: {e}")
    return Response(wav, media_type="audio/wav", headers={"Cache-Control": "private, max-age=3600"})


# ---------------- uploads (photos, videos, any file) ----------------
UPLOADS_DIR = os.path.join(os.path.dirname(C.PATH), "uploads")


@app.post("/api/upload", dependencies=[Depends(auth)])
async def upload(request: Request, name: str):
    """Raw body upload (no multipart): saved under ~/.config/atenea/uploads/YYYY-MM/."""
    safe = re.sub(r"[^\w.\- ]+", "_", os.path.basename(name)).strip() or "file"
    d = os.path.join(UPLOADS_DIR, time.strftime("%Y-%m"))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{uuid.uuid4().hex[:6]}-{safe}")
    size = 0
    with open(path, "wb") as f:
        async for chunk in request.stream():
            size += len(chunk)
            if size > 4 * 1024**3:
                f.close()
                os.remove(path)
                raise HTTPException(413, "File too large (max 4 GB)")
            f.write(chunk)
    return {"path": path, "name": safe, "size": size, "type": mimetypes.guess_type(path)[0] or "application/octet-stream"}


# ---------------- artifacts & media ----------------
SANDBOX = {"Content-Security-Policy": "sandbox allow-scripts allow-popups allow-forms allow-modals allow-downloads",
           "Cache-Control": "no-store"}


@app.get("/api/artifacts/{aid}", dependencies=[Depends(auth)])
def artifact_meta(aid: str):
    m = A.get(aid)
    if not m:
        raise HTTPException(404, "Artifact not found")
    return m


@app.get("/a/{aid}")
def artifact_page(aid: str, request: Request, v: int | None = None, download: int = 0):
    """The page itself. Sandboxed (opaque origin): it can run scripts but cannot reach the panel's API or cookies."""
    if not _valid(request.cookies.get(COOKIE)):
        return RedirectResponse("/login")
    f = A.file_for(aid, v)
    if not f:
        raise HTTPException(404, "Artifact not found")
    headers = dict(SANDBOX)
    if download:
        title = re.sub(r"[^\w\- ]+", "", (A.get(aid) or {}).get("title", "artifact")).strip() or "artifact"
        headers["Content-Disposition"] = f'attachment; filename="{title}.html"'
    return FileResponse(f, media_type="text/html; charset=utf-8", headers=headers)


MEDIA_ROOTS = [os.path.expanduser("~"), "/mnt/models", "/tmp"]


@app.get("/api/file", dependencies=[Depends(auth)])
def local_file(path: str, download: int = 0):
    """Images, videos, PDFs and other files from this machine, so Atenea can show them in the chat."""
    real = os.path.realpath(os.path.expanduser(path))
    if not any(real == r or real.startswith(r.rstrip("/") + "/") for r in MEDIA_ROOTS):
        raise HTTPException(403, "Outside the allowed folders")
    cfg_dir = os.path.realpath(os.path.dirname(C.PATH))
    private = {os.path.realpath(C.PATH), os.path.join(cfg_dir, "push.json"), os.path.join(cfg_dir, "cli.json"),
               os.path.join(cfg_dir, "config.json")}
    if (os.path.basename(real).startswith(".") or real in private or "/.ssh/" in real or "/.gnupg/" in real
            or "/.config/ornith/" in real or real.startswith("/etc/")):
        raise HTTPException(403, "Private file")
    if not os.path.isfile(real):
        raise HTTPException(404, "File not found")
    mt = mimetypes.guess_type(real)[0] or "application/octet-stream"
    headers = {"Cache-Control": "private, max-age=300"}
    if download:
        name = re.sub(r"^[0-9a-f]{6}-", "", os.path.basename(real))
        headers["Content-Disposition"] = f'attachment; filename="{re.sub(r"[^\w.\- ]+", "_", name)}"'
    if mt in ("text/html", "image/svg+xml"):
        headers.update(SANDBOX)                         # never run local HTML/SVG with the panel's origin
    return FileResponse(real, media_type=mt, headers=headers)


# ---------------- settings & status ----------------
@app.get("/api/config", dependencies=[Depends(auth)])
def get_config():
    cfg = C.load()
    if cfg["telegram"].get("token"):
        cfg["telegram"]["token"] = cfg["telegram"]["token"][:8] + "…(hidden)"
    cfg.pop("web", None)
    cfg.setdefault("default_mode", "auto")
    cfg["model"].setdefault("context", 204800)
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
            "ornith": _sh("systemctl is-active ornith"), "telegram": _sh("systemctl is-active atenea-telegram"),
            "ram_used": _sh("free -m | awk '/Mem/{print $3}'"), "ram_total": _sh("free -m | awk '/Mem/{print $2}'"),
            "gpu_temp": _sh("sensors | awk '/edge/{gsub(/[+°C]/,\"\",$2); print $2}'"),
            "gpu_power": _sh("sensors | awk '/PPT/{print $2}'"),
            "disk_used": _sh("df -BG /mnt/models | awk 'NR==2{print $3}'"),
            "disk_total": _sh("df -BG /mnt/models | awk 'NR==2{print $2}'"),
            "uptime": _sh("uptime -p"), "projects": list(cfg.get("projects", {}).keys()),
            "busy": sum(1 for j in JOBS.values() if not j["done"]), "panel_url": _panel_url()}


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
    return FileResponse(f, media_type=mimetypes.guess_type(f)[0] or "application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def apple_icon():
    return FileResponse(os.path.join(HERE, "brand", "icon-180.png"), media_type="image/png")


@app.get("/manifest.webmanifest")
def manifest():
    name = C.load().get("assistant_name", "Atenea")
    return Response(json.dumps({
        "name": name, "short_name": name, "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#111315", "theme_color": "#111315",
        "icons": [{"src": "/brand/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/brand/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}]}),
        media_type="application/manifest+json")


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
    resume_interrupted()
    threading.Thread(target=_scheduler_loop, daemon=True).start()
    threading.Thread(target=_kb_loop, daemon=True).start()
    import uvicorn
    uvicorn.run(app, host=cfg["web"]["host"], port=int(cfg["web"]["port"]), log_level="warning")


if __name__ == "__main__":
    main()
