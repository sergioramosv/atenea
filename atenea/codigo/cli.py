"""Atenea Código — the terminal CLI (like Claude Code).

    atenea                      interactive session in the current folder
    atenea "fix the tests"      start the session with that message
    atenea -p "question"        print mode: answer and exit (scripts, pipes)
    atenea -c                   continue the last conversation of this folder
    atenea -r                   pick an earlier conversation to resume

The conversation lives in the Atenea server (atenea-web), so it also appears in the web / iPhone app: you can
follow it, approve actions or write from there, and the terminal shows it live ("remote control").
Shift+Tab switches the permission mode, Ctrl+C stops the current task, Ctrl+D (or /exit) quits."""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

from .. import config as C

# ---------- colours ----------
TTY = sys.stdout.isatty()
def _c(code):
    return (lambda s: f"\033[{code}m{s}\033[0m") if TTY else (lambda s: s)
DIM, BOLD, GREEN, AMBER, RED, BLUE, CYAN, GRAY = (_c("2"), _c("1"), _c("38;5;72"), _c("38;5;179"), _c("38;5;167"),
                                                 _c("38;5;74"), _c("38;5;116"), _c("38;5;245"))
MODES = ["ask", "edits", "auto", "plan"]
MODE_LABEL = {"ask": "Ask first", "edits": "Accept edits", "auto": "Auto", "plan": "Plan"}
MODE_COLOR = {"ask": GRAY, "edits": GREEN, "auto": AMBER, "plan": BLUE}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ---------- API client ----------
CLI_FILE = os.path.expanduser("~/.config/atenea/cli.json")    # remote mode: {"server": ..., "cookie": ...}


def _remote_conf() -> dict:
    try:
        return json.load(open(CLI_FILE))
    except (OSError, ValueError):
        return {}


class Api:
    """Local mode (on the server machine): token from the shared config. Remote mode (e.g. a Mac): the server URL
    and session cookie saved by `atenea login`; then this machine also runs the file/shell tools (a "runner")."""

    def __init__(self):
        rc = _remote_conf()
        self.remote = bool(os.environ.get("ATENEA_SERVER") or rc.get("server"))
        self.headers = {"Content-Type": "application/json"}
        if self.remote:
            self.base = (os.environ.get("ATENEA_SERVER") or rc["server"]).rstrip("/")
            if rc.get("cookie"):
                self.headers["Cookie"] = rc["cookie"]
        else:
            cfg = C.load()
            if not cfg["web"].get("cli_token"):             # same token the server accepts (shared config file)
                cfg["web"]["cli_token"] = secrets.token_hex(24)
                C.save(cfg)
            self.headers["Authorization"] = f"Bearer {cfg['web']['cli_token']}"
            self.base = f"http://127.0.0.1:{cfg['web'].get('port', 8787)}"

    def call(self, method: str, path: str, body=None, timeout=30):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode() if body is not None else None,
                                     self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                detail = json.load(e).get("detail")
            except Exception:
                detail = None
            if e.code == 401 and self.remote:
                detail = "not signed in to Atenea: run `atenea login`"
            raise RuntimeError(detail or f"HTTP {e.code}") from None

    get = lambda self, p: self.call("GET", p)
    post = lambda self, p, b=None: self.call("POST", p, b if b is not None else {})
    patch = lambda self, p, b: self.call("PATCH", p, b)


# ---------- terminal markdown (line by line, so it can stream) ----------
class Md:
    def __init__(self, out=None):
        self.buf, self.code, self.out = "", False, out or sys.stdout

    def _line(self, ln: str) -> str:
        if ln.strip().startswith("```"):
            self.code = not self.code
            return GRAY("  " + "─" * 40) if TTY else ln
        if self.code:
            return CYAN("  " + ln)
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            return BOLD(GREEN(m.group(2)))
        ln = re.sub(r"`([^`]+)`", lambda x: CYAN(x.group(1)), ln)
        ln = re.sub(r"\*\*([^*]+)\*\*", lambda x: BOLD(x.group(1)), ln)
        ln = re.sub(r"^(\s*)[-*] ", lambda x: x.group(1) + "• ", ln)
        ln = re.sub(r"\[\[artifact:(\w+)\]\]", lambda x: BLUE(f"[artifact {x.group(1)} — open it in the app]"), ln)
        return ln

    def feed(self, s: str):
        self.buf += s
        while "\n" in self.buf:
            ln, self.buf = self.buf.split("\n", 1)
            self.out.write(self._line(ln) + "\n")
        self.out.flush()

    def flush(self):
        if self.buf:
            self.out.write(self._line(self.buf) + "\n")
            self.buf = ""
        self.out.flush()


def short(x, n=90) -> str:
    s = " ".join(str(x).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_line(e: dict) -> str:
    d = e.get("data") or {}
    if e["kind"] == "tool_call":
        args = d.get("args", "")
        try:
            a = ast.literal_eval(args) if args.startswith("{") else {}     # repr of a dict sent by the server
        except Exception:
            a = {}
        main = a.get("command") or a.get("path") or a.get("query") or a.get("url") or a.get("pattern") or \
            a.get("task") or a.get("title") or a.get("name") or args
        return f"{GREEN('⏺')} {BOLD(d.get('name', ''))}{GRAY('(' + short(main, 80) + ')')}"
    if e["kind"] == "subagent":
        return f"{BLUE('⑂')} subagent {BOLD(d.get('agent', ''))} {GRAY(short(d.get('task', ''), 80))}"
    return f"{AMBER('!')} {GRAY(short(d.get('text', ''), 100))}"


def print_diff(diff: str):
    for ln in diff.splitlines()[:120]:
        if ln.startswith("+") and not ln.startswith("+++"):
            print(GREEN("  " + ln))
        elif ln.startswith("-") and not ln.startswith("---"):
            print(RED("  " + ln))
        else:
            print(GRAY("  " + ln))


# ---------- session ----------
class Session:
    def __init__(self, api: Api, cwd: str, mode: str, chat: str | None, interactive: bool):
        self.api, self.cwd, self.mode, self.chat, self.interactive = api, cwd, mode, chat, interactive
        self.url = None
        self.handled_jobs: set[str] = set()
        self.streaming = threading.Lock()
        self.usage = {}
        self.ask = None                       # function(prompt) -> str, set by the interactive loop

    # chat lifecycle
    def ensure_chat(self):
        if self.api.remote:
            if not getattr(self, "runner", None):
                import platform as _pf
                r = self.api.post("/api/runners", {"name": _pf.node().split(".")[0] or "computer", "cwd": self.cwd,
                                                   "platform": f"{_pf.system()} {_pf.release()}", "chat": self.chat})
                self.runner, self.chat, self.url = r["runner"], r["chat"], r["url"]
                if self.mode:
                    self.set_mode(self.mode)
                threading.Thread(target=run_tools, args=(self,), daemon=True).start()
            return
        if not self.chat:
            r = self.api.post("/api/chats", {"project": self.cwd, "mode": self.mode, "client": "cli"})
            self.chat, self.url = r["id"], r["url"]
        elif not self.url:
            self.url = f"{self.api.get('/api/status').get('panel_url', '')}/c/{self.chat}"

    def set_mode(self, m: str):
        self.mode = m
        if self.chat:
            try:
                self.api.patch(f"/api/chats/{self.chat}", {"mode": m})
            except Exception:
                pass

    def send(self, text: str) -> str | None:
        self.ensure_chat()
        if self.api.remote:
            text = expand_mentions(text, self.cwd)           # @files live on this computer
        r = self.api.post("/api/chat", {"text": text, "chat": self.chat, "mode": self.mode, "client": "cli"})
        if r.get("queued"):
            print(GRAY(f"  queued (#{r.get('position')}) — it runs when the current task ends"))
            return None
        return r["job"]

    # streaming a job (ours or one started from the app)
    def stream(self, jid: str, remote: bool = False):
        with self.streaming:
            self._stream(jid, remote)

    def _stream(self, jid: str, remote: bool):
        self.handled_jobs.add(jid)
        md, seen_ev, turn_text, t_spin, i = Md(), 0, "", 0.0, 0
        status_shown = False
        last_q = last_a = None

        def clear_status():
            nonlocal status_shown
            if status_shown and TTY:
                sys.stdout.write("\r\033[K")
                status_shown = False

        while True:
            try:
                j = self.api.get(f"/api/jobs/{jid}")
            except RuntimeError as e:
                clear_status()
                if "Not Found" in str(e) or "404" in str(e):       # server restarted: the task resumes on its own
                    print(GRAY("  (the server restarted; the task resumes automatically — follow it in the app)"))
                    return
                time.sleep(1)
                continue
            except KeyboardInterrupt:
                self._stop(jid)
                continue
            try:
                evs = j.get("events") or []
                if len(evs) > seen_ev:
                    clear_status()
                    md.flush()
                    for e in evs[seen_ev:]:
                        print("  " + tool_line(e))
                    seen_ev = len(evs)
                live = (j.get("live") or {}).get("text") or ""
                if len(live) < len(turn_text):                     # a new model turn started
                    md.flush()
                    turn_text = ""
                if live[len(turn_text):]:
                    clear_status()
                    md.feed(live[len(turn_text):])
                    turn_text = live
                if j.get("todos") and j["todos"] != getattr(self, "_todos", None):
                    self._todos = j["todos"]
                    clear_status()
                    md.flush()
                    done = sum(t["status"] == "completed" for t in j["todos"])
                    print(GRAY(f"  Tasks {done}/{len(j['todos'])}"))
                    for t in j["todos"]:
                        mark = GREEN("✓") if t["status"] == "completed" else AMBER("◐") if t["status"] == "in_progress" else GRAY("○")
                        print(f"    {mark} {GRAY(t['content']) if t['status'] == 'completed' else t['content']}")
                ap = j.get("approval")
                if ap and ap is not last_a and json.dumps(ap) != json.dumps(last_a):
                    last_a = ap
                    clear_status()
                    md.flush()
                    self._approval(jid, ap, remote)
                q = j.get("question")
                if q and json.dumps(q) != json.dumps(last_q):
                    last_q = q
                    clear_status()
                    md.flush()
                    self._question(jid, q, remote)
                if j.get("done"):
                    clear_status()
                    res = j.get("result") or ""
                    if turn_text and res.startswith(turn_text):
                        md.feed(res[len(turn_text):])
                    elif not turn_text:
                        md.feed(res)
                    md.flush()
                    u, m = j.get("usage") or {}, j.get("meta") or {}
                    self.usage = u or self.usage
                    bits = [f"{m.get('secs', j.get('secs'))}s"]
                    if u.get("tps"):
                        bits.append(f"{u['tps']} tok/s")
                    if u.get("ctx") and u.get("max"):
                        bits.append(f"context {round(100 * u['ctx'] / u['max'])}%")
                    if j.get("artifacts"):
                        bits.append(f"{len(j['artifacts'])} artifact(s) in the app")
                    print(GRAY("  · " + " · ".join(bits)))
                    if j.get("next"):
                        print(GRAY("\n  ▸ next queued message"))
                        jid = j["next"]
                        self.handled_jobs.add(jid)
                        md, seen_ev, turn_text, last_q, last_a = Md(), 0, "", None, None
                        continue
                    return
                if TTY and time.time() - t_spin > 0.12 and not (live and not live.endswith("\n") and md.buf):
                    t_spin, i = time.time(), i + 1
                    think = (j.get("live") or {}).get("thinking") or ""
                    label = "Writing" if live else ("Thinking" if think or not evs else "Working")
                    tail = short(think.splitlines()[-1] if think.strip() else "", 60)
                    sys.stdout.write(f"\r\033[K{AMBER(SPIN[i % len(SPIN)])} {label}… {GRAY(str(j.get('secs')) + 's')}"
                                     f"{GRAY('  ' + tail) if tail and not live else ''}{GRAY('  (ctrl+c to stop)')}")
                    sys.stdout.flush()
                    status_shown = True
                time.sleep(0.15)
            except KeyboardInterrupt:
                clear_status()
                self._stop(jid)

    def _stop(self, jid: str):
        try:
            r = self.api.post(f"/api/jobs/{jid}/stop")
            print(AMBER("\n  Stopping…") + (GRAY(f" ({len(r.get('queued') or [])} queued message(s) cancelled)") if r.get("queued") else ""))
        except Exception:
            pass

    def _approval(self, jid: str, ap: dict, remote: bool):
        print(AMBER("  ⏸ Allow ") + BOLD(ap["tool"]) + AMBER("?"))
        if ap.get("diff"):
            print_diff(ap["diff"])
        else:
            print(GRAY("  " + short(ap.get("args", ""), 600)))
        if not self.interactive:                    # print mode (scripts): nobody to ask, deny and carry on
            self.api.post(f"/api/jobs/{jid}/approve/no")
            print(GRAY("  → denied (print mode; use --mode auto or run interactively)"))
            return
        if remote or not self.ask:
            print(GRAY("  Answer in the app, or type /allow, /always or /deny"))
            self.pending_approval = jid
            return
        opts = "[y] yes  [n] no" + (f"  [a] {ap['always']}" if ap.get("always") else "")
        ans = (self.ask(f"  {opts} › ") or "").strip().lower()
        a = "always" if ans.startswith("a") and ap.get("always") else "yes" if ans.startswith(("y", "s")) else "no"
        self.api.post(f"/api/jobs/{jid}/approve/{a}")
        print(GRAY(f"  → {a}"))

    def _question(self, jid: str, q: dict, remote: bool):
        print(BLUE("  ? ") + BOLD(q["question"]))
        for n, o in enumerate(q.get("options") or [], 1):
            print(f"    {BOLD(str(n))}. {o}")
        if remote or not self.ask:
            print(GRAY("  Answer in the app, or type /answer <text>"))
            self.pending_question = jid
            return
        ans = (self.ask("  › ") or "").strip()
        opts = q.get("options") or []
        if ans.isdigit() and 1 <= int(ans) <= len(opts):
            ans = opts[int(ans) - 1]
        self.api.post(f"/api/jobs/{jid}/answer", {"answer": ans or "(no answer)"})


# ---------- runner: execute Atenea's file & shell tools on this computer ----------
def run_tools(s: "Session"):
    from .. import tools as T
    while True:
        try:
            r = s.api.call("GET", f"/api/runners/{s.runner}/next?wait=25", timeout=40)
        except RuntimeError as e:
            if "Unknown runner" in str(e):                 # the server restarted: register again, same chat
                s.runner = None
                try:
                    s.ensure_chat()
                except Exception:
                    time.sleep(5)
                return
            time.sleep(3)
            continue
        except Exception:
            time.sleep(3)
            continue
        call = r.get("call")
        if not call:
            continue
        tool, args = call["tool"], call.get("args") or {}
        try:
            if tool == "read_raw":
                p = os.path.join(s.cwd, os.path.expanduser(args["path"]))
                out = open(p, encoding="utf-8").read() if os.path.exists(p) else "\0missing"
            elif tool == "read_b64":
                import base64
                p = os.path.join(s.cwd, os.path.expanduser(args["path"]))
                out = base64.b64encode(open(p, "rb").read()).decode() if os.path.isfile(p) else "\0missing"
            elif tool == "restore_raw":
                p = os.path.join(s.cwd, os.path.expanduser(args["path"]))
                if args.get("content") is None:
                    if os.path.exists(p):
                        os.remove(p)
                else:
                    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
                    open(p, "w", encoding="utf-8").write(args["content"])
                out = "ok"
            else:
                out = T.run(tool, args, s.cwd)
        except Exception as e:
            out = f"Error: {type(e).__name__}: {e}"
        try:
            s.api.post(f"/api/runners/{s.runner}/result", {"id": call["id"], "output": str(out)})
        except Exception:
            pass


def expand_mentions(text: str, cwd: str) -> str:
    """`@path` attaches that file from this computer (same as the server does for local chats)."""
    extra = []
    for m in re.finditer(r"(?<![\w@])@([\w./~-][^\s,;:!?()]*)", text):
        rel = m.group(1).rstrip(".")
        p = os.path.realpath(os.path.join(cwd, os.path.expanduser(rel)))
        try:
            if os.path.isfile(p) and os.path.getsize(p) < 150_000:
                extra.append(f'<file path="{rel}">\n{open(p, encoding="utf-8").read()}\n</file>')
            elif os.path.isdir(p):
                extra.append(f'<directory path="{rel}">\n' + "\n".join(sorted(os.listdir(p))[:300]) + "\n</directory>")
        except (OSError, UnicodeDecodeError):
            pass
    return text + ("\n\n" + "\n\n".join(extra[:10]) if extra else "")


def login(server: str | None):
    import getpass
    server = (server or input("Atenea server [https://zeus.tailbf0b1c.ts.net]: ").strip()
              or "https://zeus.tailbf0b1c.ts.net").rstrip("/")
    pw = getpass.getpass("Password: ")
    req = urllib.request.Request(server + "/api/login", json.dumps({"password": pw}).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            cookie = (r.headers.get("set-cookie") or "").split(";")[0]
    except urllib.error.HTTPError as e:
        print(RED(f"Login failed: {e.code}"))
        return 1
    except urllib.error.URLError as e:
        print(RED(f"Cannot reach {server}: {e.reason}. Is Tailscale on?"))
        return 1
    os.makedirs(os.path.dirname(CLI_FILE), exist_ok=True)
    json.dump({"server": server, "cookie": cookie}, open(CLI_FILE, "w"))
    os.chmod(CLI_FILE, 0o600)
    print(GREEN("✓ Signed in.") + " Now run " + BOLD("atenea") + " in any folder.")
    return 0


# ---------- interactive loop ----------
HELP = [("/help", "this list"), ("/mode", "ask · edits · auto · plan (or Shift+Tab)"), ("/plan /ask /edits /auto", "switch mode"),
        ("/new", "new conversation in this folder"), ("/resume", "pick an earlier conversation"),
        ("/remote", "link + QR to continue on the phone / web"), ("/context", "what fills the context"),
        ("/compact", "summarise the conversation"), ("/memory", "what Atenea remembers"), ("/remember <x>", "remember something"),
        ("/tasks", "background shells, subagents, monitors"), ("/doctor", "check everything works"),
        ("/equipo <tarea>", "Atenea Equipo: plan → programa → revisa → prueba"),
        ("/allow /always /deny", "answer an approval asked from the app"), ("/answer <text>", "answer a question"),
        ("/exit", "quit (the conversation stays in the app)")]


def banner(s: Session, status: dict):
    w = min(shutil.get_terminal_size((90, 20)).columns, 92)
    print(GREEN("╭" + "─" * (w - 2) + "╮"))
    print(GREEN("│ ") + BOLD("✻ Atenea Código") + GRAY(f"  ·  Ornith (local) on ZEUS  ·  {'🟢 online' if status.get('ornith') == 'active' else '🔴 model offline'}"))
    print(GREEN("│ ") + GRAY("folder ") + s.cwd.replace(os.path.expanduser("~"), "~")
          + (GRAY("  (files & commands run on this computer)") if s.api.remote else ""))
    print(GREEN("│ ") + GRAY("mode   ") + MODE_COLOR[s.mode](MODE_LABEL[s.mode]) + GRAY("  (shift+tab to switch)"))
    if s.url:
        print(GREEN("│ ") + GRAY("app    ") + BLUE(s.url) + GRAY("  (/remote for a QR)"))
    print(GREEN("╰" + "─" * (w - 2) + "╯"))
    print(GRAY("  /help for commands · @file to attach · ctrl+c stops · ctrl+d quits\n"))


def show_history(s: Session):
    c = s.api.get(f"/api/chats/{s.chat}")
    for e in c["log"][-6:]:
        print(BOLD("› ") + short(e["q"], 300))
        md = Md()
        md.feed((e.get("a") or "")[:3000])
        md.flush()
        print()
    return c


def interactive(s: Session, first: str | None):
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.patch_stdout import patch_stdout

    status = {}
    try:
        status = s.api.get("/api/status")
    except Exception as e:
        print(RED(f"Atenea server not reachable at {s.api.base}: {e}\nStart it with: sudo systemctl start atenea-web"))
        return 1
    s.ensure_chat()
    banner(s, status)
    if first is None and s.chat:
        try:
            c = s.api.get(f"/api/chats/{s.chat}")
            if c["log"]:
                show_history(s)
        except Exception:
            pass

    kb = KeyBindings()

    @kb.add("s-tab")
    def _(event):
        s.set_mode(MODES[(MODES.index(s.mode) + 1) % len(MODES)])
        event.app.invalidate()

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    def toolbar():
        u = s.usage or {}
        ctx = f" · context {round(100 * u['ctx'] / u['max'])}%" if u.get("ctx") and u.get("max") else ""
        col = {"ask": "ansigray", "edits": "ansigreen", "auto": "ansiyellow", "plan": "ansiblue"}[s.mode]
        return HTML(f"<{col}>⏵⏵ {MODE_LABEL[s.mode]}</{col}> <b>·</b> shift+tab mode{ctx} · /help")

    os.makedirs(os.path.dirname(C.PATH), exist_ok=True)
    words = [h[0].split()[0] for h in HELP] + ["/plan", "/ask", "/edits", "/auto", "/always", "/deny", "/bashes"]
    ps = PromptSession(history=FileHistory(os.path.join(os.path.dirname(C.PATH), "cli_history")), key_bindings=kb,
                       bottom_toolbar=toolbar, completer=WordCompleter(words, sentence=True), complete_while_typing=True)
    s.ask = lambda p: ps.prompt(p)

    # remote control: show what is sent from the app while we wait at the prompt
    stop = threading.Event()

    def watch():
        while not stop.is_set():
            time.sleep(2)
            if s.streaming.locked() or not s.chat:
                continue
            try:
                c = s.api.get(f"/api/chats/{s.chat}")
            except Exception:
                continue
            jid = c.get("job")
            if os.environ.get("ATENEA_CLI_DEBUG"):
                open("/tmp/atenea-cli-debug.log", "a").write(f"{time.strftime('%X')} watch job={jid} handled={jid in s.handled_jobs}\n")
            if jid and jid not in s.handled_jobs:
                print("\n" + BLUE("📱 from the app: ") + BOLD(short(c.get("pending") or "", 200)))
                s.stream(jid, remote=True)
    threading.Thread(target=watch, daemon=True).start()

    def run(text: str):
        jid = s.send(text)
        if jid:
            s.stream(jid)

    try:
        if first:
            print(BOLD("› ") + first)
            run(first)
        while True:
            try:
                with patch_stdout(raw=True):
                    text = ps.prompt(HTML("<b><ansigreen>›</ansigreen></b> "), multiline=False)
            except KeyboardInterrupt:
                continue
            text = text.strip()
            if not text:
                continue
            if text.startswith("/"):
                if handle_command(s, text) == "exit":
                    break
                continue
            run(text)
    except EOFError:
        pass
    finally:
        stop.set()
    if s.url:
        print(GRAY(f"\nConversation saved — continue with `atenea -c` here or in the app: {s.url}"))
    return 0


def handle_command(s: Session, text: str):
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.lower(), arg.strip()
    api = s.api
    try:
        if cmd in ("/exit", "/quit"):
            return "exit"
        if cmd == "/help":
            for c, d in HELP:
                print(f"  {BOLD(c):<34} {GRAY(d)}")
        elif cmd in ("/plan", "/ask", "/edits", "/auto") or cmd == "/mode":
            m = cmd[1:] if cmd != "/mode" else (arg or MODES[(MODES.index(s.mode) + 1) % len(MODES)])
            if m in MODES:
                s.set_mode(m)
                print(GRAY("  mode: ") + MODE_COLOR[m](MODE_LABEL[m]))
        elif cmd == "/new":
            s.chat = s.url = None
            s.ensure_chat()
            print(GRAY(f"  new conversation · {s.url}"))
        elif cmd == "/resume":
            pick_chat(s)
        elif cmd == "/remote":
            s.ensure_chat()
            print(GRAY("  Open this on the phone (Tailscale on) or any browser:\n  ") + BLUE(s.url))
            try:
                import qrcode
                q = qrcode.QRCode(border=1)
                q.add_data(s.url)
                q.print_ascii(invert=True)
            except Exception:
                pass
        elif cmd == "/context":
            r = api.get(f"/api/context?chat={s.chat}")
            print(BOLD(f"  Context {r['total']:,} / {r['max']:,} tokens ({round(100 * r['total'] / r['max'])}%)"))
            for c in r["categories"]:
                if c["tokens"]:
                    bar = "█" * max(1, round(30 * c["tokens"] / max(1, r["total"])))
                    print(f"  {c['name']:<22} {GREEN(bar)} {GRAY(format(c['tokens'], ','))}")
        elif cmd == "/compact":
            r = api.post(f"/api/chats/{s.chat}/compact")
            print(GRAY("  Compacted. Summary kept:\n") + (r.get("summary") or "")[:1500])
        elif cmd == "/memory":
            for f in api.get("/api/memory")["facts"] or ["(nothing yet)"]:
                print("  • " + f)
        elif cmd == "/remember" and arg:
            facts = api.get("/api/memory")["facts"]
            api.call("PUT", "/api/memory", {"facts": facts + [arg]})
            print(GRAY("  remembered"))
        elif cmd in ("/tasks", "/bashes"):
            r = api.get(f"/api/activity?chat={s.chat}")
            for x in r["shells"]:
                print(f"  ▸ shell {BOLD(x['name'])} {GRAY(x['status'])}")
            for x in r["agents"]:
                print(f"  ⑂ agent {BOLD(x['name'])} {GRAY(x['status'])} {GRAY(short(x['task'], 60))}")
            for x in r["monitors"]:
                print(f"  🔔 monitor {BOLD(x['name'])} {GRAY(x['status'])}")
            if not (r["shells"] or r["agents"] or r["monitors"]):
                print(GRAY("  nothing running in the background"))
        elif cmd == "/doctor":
            for c in api.get("/api/doctor")["checks"]:
                print(f"  {'🟢' if c['ok'] else '🔴'} {c['name']} {GRAY('· ' + c['detail'])}")
        elif cmd in ("/allow", "/always", "/deny"):
            jid = getattr(s, "pending_approval", None) or (api.get(f"/api/chats/{s.chat}").get("job"))
            if jid:
                answer = {"/allow": "yes", "/always": "always", "/deny": "no"}[cmd]
                api.post(f"/api/jobs/{jid}/approve/{answer}")
                print(GRAY("  sent"))
        elif cmd == "/equipo" and arg:
            jid = s.send("Usa Atenea Equipo (la herramienta equipo) para esta tarea: " + arg)
            if jid:
                s.stream(jid)
        elif cmd == "/answer":
            jid = getattr(s, "pending_question", None) or (api.get(f"/api/chats/{s.chat}").get("job"))
            if jid:
                api.post(f"/api/jobs/{jid}/answer", {"answer": arg})
                print(GRAY("  sent"))
        else:
            jid = s.send(text)                     # other /commands (custom ones, /init, /review…) go to Atenea as text
            if jid:
                s.stream(jid)
    except RuntimeError as e:
        print(RED(f"  {e}"))


def pick_chat(s: Session):
    chats = s.api.get("/api/chats")["chats"]
    here = [c for c in chats if c.get("project") == s.cwd] + [c for c in chats if c.get("project") != s.cwd]
    for n, c in enumerate(here[:15], 1):
        where = "here" if c.get("project") == s.cwd else (c.get("project") or "default folder")
        print(f"  {BOLD(str(n)):>4}. {short(c['title'], 50):<52} {GRAY(where)}")
    try:
        a = input("  number › ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if a.isdigit() and 1 <= int(a) <= len(here[:15]):
        s.chat, s.url = here[int(a) - 1]["id"], None
        s.ensure_chat()
        show_history(s)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["login"]:
        return login(argv[1] if len(argv) > 1 else None)
    if argv[:1] == ["logout"]:
        if os.path.exists(CLI_FILE):
            os.remove(CLI_FILE)
        print("Signed out.")
        return 0
    if argv[:1] == ["update"]:
        server = (_remote_conf().get("server") or "https://zeus.tailbf0b1c.ts.net")
        return os.system(f"curl -fsSL {server}/install.sh | ATENEA_SKIP_LOGIN=1 bash")
    ap = argparse.ArgumentParser(prog="atenea", description="Atenea Código: your local coding agent in the terminal.")
    ap.add_argument("prompt", nargs="*", help="first message (optional)")
    ap.add_argument("-p", "--print", action="store_true", help="answer and exit (non-interactive)")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the last conversation here")
    ap.add_argument("-r", "--resume", nargs="?", const="pick", help="resume a conversation (id, or pick from a list)")
    ap.add_argument("--mode", choices=MODES, default=None, help="permission mode (default: edits)")
    ap.add_argument("--cwd", default=os.getcwd())
    a = ap.parse_args(argv)
    if not _remote_conf() and not os.path.exists(C.PATH) and not os.environ.get("ATENEA_SERVER"):
        print("This computer is not connected to an Atenea server yet. Run: " + BOLD("atenea login"))
        return 1
    api = Api()
    cwd = os.path.realpath(a.cwd)
    chat = None
    mode = a.mode or ("auto" if a.print else "edits")
    try:
        if a.cont:
            mine = [c for c in api.get("/api/chats")["chats"] if c.get("project") == cwd]
            if mine:
                chat = mine[0]["id"]
        elif a.resume and a.resume != "pick":
            chat = a.resume
        if chat and not a.mode:
            mode = api.get(f"/api/chats/{chat}").get("mode") or mode
    except Exception as e:
        print(RED(f"Atenea server not reachable at {api.base}: {e}"))
        return 1
    s = Session(api, cwd, mode, chat, interactive=not a.print)
    first = " ".join(a.prompt) or None
    if not sys.stdin.isatty() and a.print:
        piped = sys.stdin.read()
        first = (first + "\n\n" if first else "") + piped
    if a.print:
        if not first:
            print("Nothing to do: give a prompt, e.g. atenea -p \"summarise this repo\"")
            return 2
        jid = s.send(first)
        if jid:
            s.stream(jid)
        return 0
    if a.resume == "pick":
        s.ensure_chat()
        pick_chat(s)
    return interactive(s, first)


if __name__ == "__main__":
    sys.exit(main() or 0)
