"""Atenea Telegram: talk to Atenea from your phone.

- Only answers the users in config["telegram"]["allowed_users"] (numeric IDs).
- One agent session per chat (saved in ~/.config/atenea/sessions/<chat>.json).
- Shows progress by editing a message; dangerous actions ask for confirmation with buttons.
Commands: /new  /project <alias|path>  /status  /cancel  /help
Run:  python -m atenea.telegram
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request

from .. import config as C
from ..runtime import build_agent

SESS = os.path.expanduser("~/.config/atenea/sessions")


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, http_timeout: int = 60, **params):
        data = urllib.parse.urlencode({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                                       for k, v in params.items() if v is not None}).encode()
        try:
            return json.load(urllib.request.urlopen(self.base + method, data, timeout=http_timeout)).get("result")
        except Exception as e:
            print(f"[telegram] {method}: {e}", flush=True)
            return None

    def send(self, chat, text, **kw):
        out = None
        for i in range(0, max(len(text), 1), 4000):            # Telegram allows ~4096 characters
            out = self.call("sendMessage", chat_id=chat, text=text[i:i + 4000] or "…", **kw)
        return out

    def edit(self, chat, msg_id, text, **kw):
        return self.call("editMessageText", chat_id=chat, message_id=msg_id, text=text[:4000] or "…", **kw)


class ChatSession:
    def __init__(self, bot: "Atenea Telegram", chat: int):
        self.bot, self.chat = bot, chat
        self.cwd = C.load()["workspace"]
        self.agent = None
        self.busy = self.cancel = False
        self.pending: dict[str, threading.Event] = {}
        self.answers: dict[str, bool] = {}
        self.progress_id = None
        self.lines: list[str] = []
        self.last_edit = 0.0

    def path(self):
        return os.path.join(SESS, f"{self.chat}.json")

    def ensure_agent(self):
        if self.agent is None:
            self.agent = build_agent(self.cwd, on_event=self.on_event, approve=self.approve)
            if os.path.exists(self.path()):
                try:
                    if json.load(open(self.path())).get("cwd") == self.agent.cwd:
                        self.agent.load(self.path())
                except Exception:
                    pass
        return self.agent

    # --- live progress ---
    def on_event(self, kind, data):
        if self.cancel:
            raise KeyboardInterrupt("cancelled")
        icon = {"tool_call": "🔧", "notice": "⚠️", "subagent": "👥"}.get(kind)
        if not icon:
            return
        if kind == "tool_call":
            a = data.get("args", {})
            detail = a.get("command") or a.get("path") or a.get("pattern") or json.dumps(a, ensure_ascii=False)[:60]
            self.lines.append(f"{icon} {data['name']}: {str(detail)[:70]}")
        else:
            self.lines.append(f"{icon} {str(data.get('text') or data.get('task'))[:90]}")
        if time.time() - self.last_edit > 2.5 and self.progress_id:
            self.last_edit = time.time()
            self.bot.tg.edit(self.chat, self.progress_id, "⏳ Working…\n" + "\n".join(self.lines[-12:]))

    # --- confirmations ---
    def approve(self, tool, args) -> bool:
        key = f"{int(time.time() * 1000)}"
        ev = threading.Event()
        self.pending[key] = ev
        txt = f"🔐 Allow `{tool.name}`?\n{json.dumps(args, ensure_ascii=False, indent=1)[:1500]}"
        kb = {"inline_keyboard": [[{"text": "✅ Yes", "callback_data": f"ok:{key}"},
                                   {"text": "❌ No", "callback_data": f"no:{key}"}]]}
        self.bot.tg.send(self.chat, txt, reply_markup=kb)
        ok = ev.wait(timeout=600) and self.answers.get(key, False)
        self.pending.pop(key, None)
        return bool(ok)

    def run(self, text: str):
        self.busy, self.cancel, self.lines = True, False, []
        m = self.bot.tg.send(self.chat, "⏳ Thinking…")
        self.progress_id = m and m.get("message_id")
        t0 = time.time()
        try:
            agent = self.ensure_agent()
            s0 = agent.stats.requests
            answer = agent.run(text)
            os.makedirs(SESS, exist_ok=True)
            agent.save(self.path())
            self.bot.tg.edit(self.chat, self.progress_id,
                             f"✅ Done in {time.time() - t0:.0f}s ({agent.stats.requests - s0} steps)\n"
                             + "\n".join(self.lines[-6:]))
            self.bot.tg.send(self.chat, answer or "(no answer)")
        except KeyboardInterrupt:
            self.bot.tg.send(self.chat, "⛔ Cancelled.")
        except Exception as e:
            self.bot.tg.send(self.chat, f"❌ Error: {type(e).__name__}: {e}")
        finally:
            self.busy = False


def zeus_status() -> str:
    def sh(c):
        return subprocess.run(c, shell=True, capture_output=True, text=True).stdout.strip()
    ram = sh("free -g | awk '/Mem/{print $3\"/\"$2\" GB\"}'")
    temp = sh("sensors | awk '/edge/{print $2}'")
    watts = sh("sensors | awk '/PPT/{print $2, $3}'")
    return f"🖥️ ZEUS\nModel: {sh('systemctl is-active ornith')} (ornith)\nRAM: {ram}\nGPU: {temp}  {watts}"


class Atenea Telegram:
    def __init__(self):
        cfg = C.load()
        tok = cfg["telegram"].get("token")
        if not tok:
            raise SystemExit("telegram.token is missing in ~/.config/atenea/config.json")
        self.tg = Telegram(tok)
        self.allowed = {int(x) for x in cfg["telegram"].get("allowed_users") or []}
        self.sessions: dict[int, ChatSession] = {}

    def session(self, chat) -> ChatSession:
        if chat not in self.sessions:
            self.sessions[chat] = ChatSession(self, chat)
        return self.sessions[chat]

    def handle_command(self, s: ChatSession, text: str):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0]
        if cmd in ("/start", "/help"):
            self.tg.send(s.chat, f"Hi, I'm {C.load().get('assistant_name', 'Atenea')} 🦉. Tell me what you need.\n"
                                 "/new — start a new conversation\n/project <alias|path> — switch project\n"
                                 "/status — ZEUS status\n/cancel — stop the running task")
        elif cmd == "/new":
            s.agent = None
            if os.path.exists(s.path()):
                os.remove(s.path())
            self.tg.send(s.chat, "🆕 New conversation.")
        elif cmd == "/project":
            cfg = C.load()
            p = os.path.expanduser(cfg.get("projects", {}).get(arg.strip(), arg.strip()) or cfg["workspace"])
            if not os.path.isdir(p):
                self.tg.send(s.chat, f"{p} does not exist. Projects: {', '.join(cfg.get('projects', {})) or '(none)'}")
                return
            s.cwd, s.agent = p, None
            self.tg.send(s.chat, f"📁 Project: {p}")
        elif cmd == "/status":
            self.tg.send(s.chat, zeus_status())
        elif cmd == "/cancel":
            s.cancel = True
            self.tg.send(s.chat, "Cancelling…")
        else:
            self.tg.send(s.chat, "Unknown command. /help")

    def loop(self):
        offset = None
        print("[telegram] listening on Telegram…", flush=True)
        while True:
            for u in self.tg.call("getUpdates", http_timeout=75, offset=offset, timeout=60) or []:
                offset = u["update_id"] + 1
                if "callback_query" in u:
                    q = u["callback_query"]
                    if q["from"]["id"] not in self.allowed:
                        continue
                    verdict, _, key = q.get("data", "").partition(":")
                    s = self.session(q["message"]["chat"]["id"])
                    s.answers[key] = verdict == "ok"
                    if key in s.pending:
                        s.pending[key].set()
                    self.tg.call("answerCallbackQuery", callback_query_id=q["id"],
                                 text="Allowed" if verdict == "ok" else "Denied")
                    continue
                msg = u.get("message") or {}
                frm = (msg.get("from") or {}).get("id")
                text = msg.get("text") or ""
                if frm and frm not in self.allowed:
                    print(f"[telegram] ignored unauthorised user {frm}", flush=True)
                    continue
                if not text:
                    continue
                s = self.session(msg["chat"]["id"])
                if text.startswith("/"):
                    self.handle_command(s, text)
                elif s.busy:
                    self.tg.send(s.chat, "Still working on the previous task; wait or use /cancel.")
                else:
                    threading.Thread(target=s.run, args=(text,), daemon=True).start()


if __name__ == "__main__":
    Atenea Telegram().loop()
