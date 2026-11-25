"""Atenea: lightweight agent engine for local models.

Loop: the model reasons and requests tools -> tools run -> results go back into the history -> repeat,
until it answers without tools (and, if `verify` is set, until verification passes).

Lessons built in (measured on ZEUS with Ornith):
- short instructions (~400 tokens) => every step is cheap;
- keep the model's previous reasoning in the history (without it Ornith loses the thread on long tasks);
- recover from corrupted tool calls and empty/truncated replies;
- verification guard: a task is not finished while `verify` fails.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Callable

from . import hooks as H
from . import tools as T
from .llm import LLM, InvalidToolCall, ModelProfile
from .registry import Tool
from .skills import SkillSet

SYSTEM_MAIN = """You are {name}, an autonomous agent working in the directory {cwd}.
You solve the user's task using tools; nobody reviews your intermediate steps.

How to work:
- Explore what you need first (list_files, read_file, grep) and understand the code before changing it.
- Make small, precise changes with edit_file (exact replacement) or write_file for new files.
- ALWAYS verify by running the code or the tests with bash. If something fails, read the error and fix it.
- Do not modify existing tests unless asked. Do not invent files or APIs: check them.
- Be concise: do not repeat whole files in your messages.
- Reply to the user in the language they write in.
- For tasks with 3 or more steps, keep a todo list with todo_write: write it at the start, mark one item
  in_progress while you work on it and completed as soon as it is done. Skip it for quick questions.
{extra}
When the task is done and verified, reply with a short summary WITHOUT calling tools."""

DELEGATE_HINT = """- You have `delegate` to hand independent, well-scoped subtasks to subagents (same tools, same directory).
  Use it only when it saves work: several independent pieces in parallel, in different files/functions.
  State exactly what to do and what they may touch. Then verify the result yourself."""

SYSTEM_SUB = """You are a subagent of {name} in the directory {cwd}. Do EXACTLY the given subtask and touch only what you
are allowed to (other subagents may be editing other parts at the same time: if edit_file fails because the text
changed, re-read the file and retry). Verify your work with bash.
Finish with a 2-3 line summary WITHOUT calling tools."""

MSG_INVALID = ("Your last tool call was invalid (broken JSON or far too long, maybe repeated text). Repeat it in a "
               "shorter form: use edit_file for targeted changes instead of rewriting whole files.")
MSG_EMPTY = ("Your reply was cut off or empty. Continue: use the tools to make the changes (reason less, act more) "
             "and, once everything is verified, reply with a summary.")
MSG_VERIFY = ("Not finished yet: the verification `{cmd}` fails:\n{out}\n"
              "Fix the problem and verify again before finishing.")
MSG_TURNS_LEFT = ("[{n} tool turn(s) left. Use them only for what you still must check; then give your final answer "
                  "as the task asked.]")
MSG_FINAL = ("You have used all your tool turns. Do not call any tool: give your final answer now, as the task asked, "
             "with what you have found so far.")
MSG_DENIED = "The user did NOT authorize this action. Do not retry it; continue without it or ask what they prefer."
MSG_PLAN_BLOCK = ("Plan mode is on: you may only read and search. Do not modify files, run commands or take external "
                  "actions. Finish your investigation and reply with a concise, numbered plan for the user to approve.")
PLAN_NOTE = ("\n\n[Plan mode: investigate using read-only tools only, then reply with a numbered plan. "
             "Do not change anything yet.]")

# Permission modes (like Claude Code). Which tool kinds need the user's approval in each mode:
#   ask   -> edits, commands and external actions        edits -> commands and external actions
#   auto  -> only external actions (tools marked dangerous) plan -> nothing runs except read-only tools
MODES = ("ask", "edits", "auto", "plan")
READ_ONLY = {"read_file", "list_files", "grep", "load_skill", "todo_write", "ask_user", "show_card"}
EDIT_TOOLS = {"write_file", "edit_file"}


@dataclass
class Stats:
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    secs_model: float = 0.0

    def add(self, o: "Stats"):
        for k in ("requests", "prompt_tokens", "completion_tokens", "tool_calls", "secs_model"):
            setattr(self, k, getattr(self, k) + getattr(o, k))


# Events: on_event(kind, data). Kinds: "turn", "thinking", "text", "tool_call", "tool_result",
# "notice", "subagent", "done". Lets Telegram/web show progress in real time.
EventFn = Callable[[str, dict], None]
# Permissions: approve(tool, args) -> bool. Asked before running dangerous tools.
ApproveFn = Callable[[Tool, dict], bool]


@dataclass
class Agent:
    cwd: str = "."
    profile: ModelProfile = field(default_factory=ModelProfile)
    name: str = "Atenea"
    tools: list[Tool] = field(default_factory=list)        # custom (@tool) and MCP tools
    builtin: bool = True                                    # bash, read_file, write_file, edit_file, list_files, grep
    allow_delegate: bool = True
    sub_profile: ModelProfile | None = None                 # model for subagents (defaults to the same one)
    skills: SkillSet | None = None
    extra_instructions: str = ""
    verify: str | None = None                               # command that must pass before finishing
    max_turns: int = 80
    turns_warning: int = 2                                  # say "N turns left" when this many remain (0 = never)
    free_todo_turns: int = 4                                # turns that only update the todo list do not count, up to this
    on_event: EventFn | None = None
    approve: ApproveFn | None = None
    system: str | None = None
    verbose: bool = False
    mode: str = "auto"                                      # ask | edits | auto | plan (see MODES)
    stream: bool = False                                    # emit "delta" events (reasoning/text as it is written)
    before_tool: Callable[[Tool, dict], None] | None = None  # called right before a tool runs (checkpoints, hooks)
    hooks: dict | None = None                               # shell hooks per event (see atenea/hooks.py)
    agents: dict | None = None                              # named subagents for `delegate` (see atenea/subagents.py)
    spawn_bg: Callable | None = None                        # host runs background subagents: spawn_bg(sub, task, name) -> id

    def __post_init__(self):
        self.cwd = os.path.abspath(os.path.expanduser(self.cwd))
        self.llm = LLM(self.profile)
        self.stats = Stats()
        self.transcripts: list = []
        self._tools: dict[str, Tool] = {}
        if self.builtin:
            for t in T.builtin_tools(lambda: self.cwd):
                self._tools[t.name] = t
        for t in self.tools:
            self._tools[t.name] = t
        if self.skills:
            self._tools["load_skill"] = self.skills.tool()
        self.todos: list[dict] = []
        self._tools["todo_write"] = Tool(
            "todo_write", "Create or update your todo list for the current task. Pass the FULL list each time. "
            "status: pending | in_progress | completed. Only one item in_progress at a time.",
            {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "object", "properties": {
                "content": {"type": "string"}, "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                "required": ["content", "status"]}}}, "required": ["todos"]}, self._todo_write)
        extra = []
        if self.allow_delegate:
            extra.append(DELEGATE_HINT)
        if self.skills and len(self.skills):
            extra.append(self.skills.catalog())
        if self.extra_instructions:
            extra.append(self.extra_instructions)
        tmpl = self.system or SYSTEM_MAIN
        self.messages: list[dict] = [{"role": "system", "content": tmpl.format(
            name=self.name, cwd=self.cwd, extra="\n".join(extra))}]
        self._invalid = self._nudges = self._cooldown = self._verify_fails = 0
        self.context_tokens = 0                              # size of the conversation in the last request
        self.timings: dict = {}                              # llama.cpp timings of the last call (real speeds)
        self.note = ""                                       # per-message note appended to the user's message (mode…)

    # ---------- helpers ----------
    def _emit(self, kind: str, **data):
        if self.verbose:
            print(f"[{self.name}] {kind} {json.dumps(data, ensure_ascii=False)[:160]}", flush=True)
        if self.on_event:
            try:
                self.on_event(kind, data)
            except KeyboardInterrupt:
                raise
            except Exception:
                pass

    def schemas(self) -> list[dict]:
        s = [t.schema() for t in self._tools.values()]
        if self.allow_delegate:
            s.append(T.DELEGATE)
        return s

    # ---------- tools ----------
    def _run_tool(self, name: str, args: dict) -> str:
        self.stats.tool_calls += 1
        if name == "delegate":
            return self._delegate(args)
        t = self._tools.get(name)
        if not t:
            return f"Error: unknown tool '{name}'. Available: {', '.join(self._tools)}"
        need = self._needs_approval(t)
        if need == "block":
            return MSG_PLAN_BLOCK
        if need and self.approve and not self.approve(t, args):
            return MSG_DENIED
        if self.before_tool:
            try:
                self.before_tool(t, args)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                return f"Blocked before running {name}: {e}"
        for code, so, se, cmd in H.run(self.hooks, "PreToolUse", self.cwd, name, {"args": args}):
            if code == 2:
                self._emit("notice", text=f"hook blocked {name}: {cmd}")
                return f"Blocked by a PreToolUse hook ({cmd}): {(se or so).strip()[:1500]}"
        try:
            out = t(**args)
        except TypeError as e:
            return f"Error in the arguments of {name}: {e}"
        except Exception as e:
            return f"Error: {type(e).__name__}: {e}"
        out = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
        for code, so, se, cmd in H.run(self.hooks, "PostToolUse", self.cwd, name, {"args": args, "output": out[:4000]}):
            if code != 0 or so.strip():
                self._emit("notice", text=f"hook after {name}: {cmd} (exit {code})")
                out += f"\n\n[PostToolUse hook `{cmd}` exit {code}]\n{(so + se).strip()[:2000]}"
        return T.clip(out)

    def _todo_write(self, todos: list) -> str:
        clean = []
        for t in todos or []:
            if isinstance(t, dict) and str(t.get("content", "")).strip():
                st = t.get("status") if t.get("status") in ("pending", "in_progress", "completed") else "pending"
                clean.append({"content": str(t["content"])[:200], "status": st})
        self.todos = clean
        self._emit("todos", todos=clean)
        done = sum(t["status"] == "completed" for t in clean)
        return f"Todo list updated ({done}/{len(clean)} completed). Keep going."

    def _delta(self, kind: str, text: str):
        if self.on_event:
            try:
                self.on_event("delta", {"kind": kind, "text": text})
            except KeyboardInterrupt:
                raise
            except Exception:
                pass

    def _needs_approval(self, t: Tool) -> bool | str:
        """Decide from the permission mode whether a tool call needs approval (True), runs freely (False)
        or is refused because plan mode only allows reading ("block")."""
        name = t.name
        exec_ = name in ("bash", "bg_start")
        builtin = name in READ_ONLY or name in EDIT_TOOLS or exec_
        if self.mode == "plan":                         # read-only: built-in readers and safe (non-dangerous) extras
            return "block" if t.dangerous or (builtin and name not in READ_ONLY) else False
        if t.dangerous:
            return True
        if not builtin or name in READ_ONLY:
            return False
        if self.mode == "ask":
            return True                                 # edits and commands
        if self.mode == "edits":
            return exec_
        return False                                    # auto

    def _delegate(self, args: dict) -> str:
        task = args.get("task", "")
        spec = (self.agents or {}).get(args.get("agent") or "")
        if args.get("agent") and not spec:
            return f"Error: no subagent named {args['agent']!r}. Available: {', '.join(self.agents or {}) or 'none'}."
        background = bool(args.get("background")) and self.spawn_bg is not None
        self._emit("subagent", task=task[:300], agent=spec["name"] if spec else "general", background=background)
        system = SYSTEM_SUB if not spec else (spec["prompt"] + "\n\nYou work in {cwd} as a subagent of {name}. "
                                              "Do exactly the given task, then finish with a short summary WITHOUT "
                                              "calling tools.{extra}")
        sub = Agent(cwd=self.cwd, profile=replace(self.sub_profile or self.profile, extra=None),
                    name=f"{self.name}/{spec['name'] if spec else 'sub'}",
                    tools=self.tools, allow_delegate=False, max_turns=40, system=system, hooks=self.hooks,
                    on_event=self.on_event, approve=self.approve, verbose=self.verbose, mode=self.mode)
        if spec and spec.get("tools"):
            allowed = set(spec["tools"]) | {"todo_write"}
            sub._tools = {k: v for k, v in sub._tools.items() if k in allowed}
        if background:
            sid = self.spawn_bg(sub, task, spec["name"] if spec else "general")
            return (f"Background subagent {sid} started. Its result will arrive later as a new message in this "
                    "conversation; do not wait for it. Continue with other work or finish your reply.")
        res = sub.run(task)
        self.stats.add(sub.stats)
        self.transcripts.append({"task": task, "messages": sub.messages, "subs": sub.transcripts})
        return f"Subagent result:\n{res}"

    def _check(self) -> tuple[bool, str]:
        r = subprocess.run(self.verify, shell=True, cwd=self.cwd, capture_output=True, text=True,
                           timeout=600, executable="/bin/bash")
        return r.returncode == 0, T.clip((r.stdout + r.stderr)[-4000:])

    # ---------- loop ----------
    def run(self, task: str) -> str:
        """Run a task (or a new message in the same conversation) and return the final answer."""
        for code, so, se, cmd in H.run(self.hooks, "UserPromptSubmit", self.cwd, "", {"prompt": task}):
            if code == 2:
                return self._finish(f"Blocked by a UserPromptSubmit hook ({cmd}): {(se or so).strip()[:800]}")
            if so.strip():
                task += f"\n\n[context from hook `{cmd}`]\n{so.strip()[:3000]}"
        self.messages.append({"role": "user", "content": task + (PLAN_NOTE if self.mode == "plan" else "") + self.note})
        self._nudges = self._verify_fails = self._stop_hooks = 0
        used = free = turn = 0
        while used < self.max_turns:
            turn += 1
            self._emit("turn", n=turn)
            T.compact(self.messages)
            try:
                r = self.llm.chat(self.messages, self.schemas(), temperature=0.6 if self._cooldown else None,
                                  on_delta=self._delta if self.stream and self.on_event else None,
                                  on_wait=lambda: self._emit("notice", text="waiting for the model: another chat is using it"))
            except InvalidToolCall:
                self.stats.requests += 1
                self._invalid += 1
                if self._invalid > 4:
                    return self._finish("Aborted: the model produced too many invalid tool calls.")
                self._cooldown = 4
                self._emit("notice", text="invalid tool call; asking the model to repeat it")
                self.messages.append({"role": "user", "content": MSG_INVALID})
                continue
            self._cooldown = max(0, self._cooldown - 1)
            u = r["usage"]
            self.stats.requests += 1
            self.stats.prompt_tokens += u.get("prompt_tokens", 0)
            self.stats.completion_tokens += u.get("completion_tokens", 0)
            self.stats.secs_model += r["secs"]
            self.context_tokens = u.get("prompt_tokens", 0) + u.get("completion_tokens", 0)
            if r.get("timings"):
                self.timings = r["timings"]
            msg = r["message"]
            calls = msg.get("tool_calls") or []
            content = msg.get("content") or ""
            entry = {"role": "assistant", "content": content}
            if self.profile.keep_reasoning and msg.get("reasoning_content"):
                entry["reasoning_content"] = msg["reasoning_content"]
                self._emit("thinking", text=msg["reasoning_content"][-500:])
            if calls:
                entry["tool_calls"] = calls
            self.messages.append(entry)
            if content.strip():
                self._emit("text", text=content)

            if not calls:
                if (r["finish"] == "length" or not content.strip()) and self._nudges < 3:
                    self._nudges += 1
                    self._emit("notice", text="empty or truncated reply; asking the model to continue")
                    self.messages.append({"role": "user", "content": MSG_EMPTY})
                    continue
                if self.verify and self._verify_fails < 3:
                    ok, out = self._check()
                    if not ok:
                        self._verify_fails += 1
                        self._emit("notice", text=f"verification fails (attempt {self._verify_fails}); continuing")
                        self.messages.append({"role": "user", "content": MSG_VERIFY.format(cmd=self.verify, out=out)})
                        continue
                blocked = [x for x in H.run(self.hooks, "Stop", self.cwd, "", {"answer": content[:4000]}) if x[0] == 2]
                if blocked and self._stop_hooks < 3:
                    self._stop_hooks += 1
                    self._emit("notice", text=f"Stop hook says not finished ({blocked[0][3]}); continuing")
                    self.messages.append({"role": "user", "content": "A Stop hook reports the task is not finished:\n"
                                          + (blocked[0][2] or blocked[0][1]).strip()[:3000] + "\nFix it and finish."})
                    continue
                return self._finish(content)

            parsed = []
            for c in calls:
                fn = c.get("function", {})
                try:
                    a = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(a, dict):
                        raise ValueError
                except (json.JSONDecodeError, ValueError):
                    a = {"__error__": fn.get("arguments")}
                parsed.append((c, fn.get("name", ""), a))
            outs = {}
            dels = [p for p in parsed if p[1] == "delegate" and "__error__" not in p[2]]
            if len(dels) > 1:                                  # subagents run in parallel
                with ThreadPoolExecutor(len(dels)) as ex:
                    futs = {ex.submit(self._run_tool, n, a): c["id"] for c, n, a in dels}
                    for f, cid in futs.items():
                        outs[cid] = f.result()
            for c, n, a in parsed:
                if c["id"] in outs:
                    continue
                self._emit("tool_call", name=n, args=a)
                if "__error__" in a:
                    outs[c["id"]] = f"Error: invalid JSON arguments: {str(a['__error__'])[:200]}"
                else:
                    outs[c["id"]] = self._run_tool(n, a)
                self._emit("tool_result", name=n, output=outs[c["id"]][:1000])
            for c, n, a in parsed:
                self.messages.append({"role": "tool", "tool_call_id": c["id"], "content": outs[c["id"]]})
            # planning is free (up to a cap): a model that keeps a todo list should not lose turns to it
            if all(n == "todo_write" for _, n, _ in parsed) and free < self.free_todo_turns:
                free += 1
            else:
                used += 1
            left = self.max_turns - used
            if self.turns_warning and 0 < left <= self.turns_warning and self.messages[-1].get("role") == "tool":
                self.messages[-1]["content"] += "\n\n" + MSG_TURNS_LEFT.format(n=left)
        return self._final_answer()

    def _final_answer(self) -> str:
        """Out of turns: one last call that may not use tools, so the model answers instead of being cut off."""
        self.messages.append({"role": "user", "content": MSG_FINAL})
        extra = self.profile.extra
        self.profile.extra = {**(extra or {}), "tool_choice": "none"}
        try:
            r = self.llm.chat(self.messages, self.schemas())
            content = (r["message"].get("content") or "").strip()
            self.stats.requests += 1
        except Exception:
            content = ""
        finally:
            self.profile.extra = extra
        if content and "<tool_call>" not in content:
            self.messages.append({"role": "assistant", "content": content})
            return self._finish(content)
        return self._finish("Turn limit reached without finishing.")

    def _finish(self, text: str) -> str:
        self._emit("done", text=text, stats=self.stats.__dict__)
        return text

    # ---------- sessions ----------
    def save(self, path: str):
        """Save the conversation so it can be resumed later (Agent.load)."""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        json.dump({"cwd": self.cwd, "name": self.name, "messages": self.messages, "saved": time.time()},
                  open(path, "w"), ensure_ascii=False)

    def load(self, path: str) -> "Agent":
        d = json.load(open(path))
        self.messages = [self.messages[0]] + [m for m in d["messages"] if m["role"] != "system"]
        return self
