"""Built-in tools and context management."""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess

MAX_OUT = 12000          # max characters of tool output returned to the model
KEEP_RECENT = 12         # most recent messages that are never trimmed
COMPACT_CHARS = 90000    # history size (~25k tokens) above which old tool output/arguments are trimmed (once)


def _p(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


SCHEMAS = [
    _p("bash", "Run a bash command in the working directory; returns stdout+stderr and the exit code.",
       {"command": {"type": "string"}, "timeout": {"type": "integer", "description": "seconds, default 120"}}, ["command"]),
    _p("read_file", "Read a text file with line numbers. Use offset/limit for large files.",
       {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
    _p("write_file", "Create or overwrite a whole file with the given content.",
       {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _p("edit_file", "Replace an EXACT text fragment (old) with another (new) in a file. `old` must appear exactly "
       "once (include enough context). Read the file first.",
       {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}, ["path", "old", "new"]),
    _p("list_files", "List project files (recursive, ignores .git and __pycache__). Optional glob `pattern`.",
       {"path": {"type": "string"}, "pattern": {"type": "string"}}, []),
    _p("grep", "Search for a text or regex in files (grep -rn). Returns file:line:content.",
       {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
]
DELEGATE = _p("delegate", "Hand an independent, well-scoped subtask to a subagent (same tools, same directory). "
              "Several calls in the same turn run IN PARALLEL. Returns the subagent's summary.",
              {"task": {"type": "string", "description": "what to do, which files/functions it may touch, how to verify"},
               "agent": {"type": "string", "description": "optional: name of a specialised subagent (see the list in your instructions)"},
               "background": {"type": "boolean", "description": "true = run it in the background and keep going; its result arrives later as a message"}},
              ["task"])


def clip(s) -> str:
    s = s if isinstance(s, str) else str(s)
    if len(s) <= MAX_OUT:
        return s
    h = MAX_OUT // 2
    return s[:h] + f"\n... [{len(s) - MAX_OUT} characters omitted] ...\n" + s[-h:]


def _abs(cwd, path):
    return os.path.normpath(os.path.join(cwd, path or "."))


def run(name: str, a: dict, cwd: str) -> str:
    try:
        if name == "bash":
            r = subprocess.run(a["command"], shell=True, cwd=cwd, capture_output=True, text=True,
                               timeout=int(a.get("timeout") or 120), executable="/bin/bash")
            return clip(f"[exit {r.returncode}]\n{r.stdout}{r.stderr}")
        if name == "read_file":
            lines = open(_abs(cwd, a["path"]), encoding="utf-8", errors="replace").read().splitlines()
            off = max(0, int(a.get("offset") or 1) - 1)
            lim = int(a.get("limit") or 400)
            body = "\n".join(f"{i + 1:5d}\t{l}" for i, l in enumerate(lines[off:off + lim], start=off))
            more = f"\n[... {len(lines) - off - lim} more lines]" if off + lim < len(lines) else ""
            return clip(body + more) or "(empty file)"
        if name == "write_file":
            p = _abs(cwd, a["path"])
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w", encoding="utf-8").write(a["content"])
            return f"Wrote {a['path']} ({len(a['content'])} characters)."
        if name == "edit_file":
            p = _abs(cwd, a["path"])
            t = open(p, encoding="utf-8").read()
            n = t.count(a["old"])
            if n == 0:
                return "Error: `old` not found in the file (re-read it; it may have changed)."
            if n > 1:
                return f"Error: `old` appears {n} times; add more context so it is unique."
            open(p, "w", encoding="utf-8").write(t.replace(a["old"], a["new"], 1))
            return f"Edited {a['path']}."
        if name == "list_files":
            root, pat, out = _abs(cwd, a.get("path")), a.get("pattern") or "*", []
            for d, dirs, files in os.walk(root):
                dirs[:] = [x for x in dirs if x not in (".git", "__pycache__", "node_modules", ".venv")]
                for f in files:
                    rel = os.path.relpath(os.path.join(d, f), cwd)
                    if fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(rel, pat):
                        out.append(rel)
            return clip("\n".join(sorted(out)) or "(no results)")
        if name == "grep":
            r = subprocess.run(["grep", "-rnE", "--exclude-dir=.git", "--exclude-dir=__pycache__", a["pattern"],
                                _abs(cwd, a.get("path"))], capture_output=True, text=True, timeout=60)
            return clip(r.stdout.replace(cwd + "/", "") or "(no matches)")
        return f"Error: unknown tool {name}"
    except subprocess.TimeoutExpired:
        return "Error: timed out."
    except Exception as e:  # the error goes back to the model so it can fix it
        return f"Error: {type(e).__name__}: {e}"


def _size(m: dict) -> int:
    n = len(m.get("content") or "")
    for c in m.get("tool_calls") or []:
        n += len((c.get("function") or {}).get("arguments") or "")
    return n


def _slim_args(raw: str) -> str:
    """Keep the shape of a big tool call (path, short fields) but drop bulky values (file contents, HTML…)."""
    try:
        a = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw[:300] + " …[trimmed]"
    if not isinstance(a, dict):
        return raw[:300]
    return json.dumps({k: (v if not isinstance(v, str) or len(v) <= 200 else
                           f"[{len(v):,} characters omitted to save context]") for k, v in a.items()}, ensure_ascii=False)


def compact(messages: list[dict]):
    """Context editing: when the history is large, trim OLD tool outputs and bulky tool-call arguments in one pass,
    down to ~55% of the limit. Done rarely and all at once, so the cached prompt prefix stays stable afterwards."""
    if sum(_size(m) for m in messages) < COMPACT_CHARS:
        return False
    for m in messages[1:-KEEP_RECENT]:
        if m["role"] == "tool" and len(m.get("content") or "") > 300:
            m["content"] = m["content"][:200] + "\n[... old output trimmed to save context; run the tool again if needed ...]"
        for c in m.get("tool_calls") or []:
            f = c.get("function") or {}
            if len(f.get("arguments") or "") > 600:
                f["arguments"] = _slim_args(f["arguments"])
        if sum(_size(x) for x in messages) < COMPACT_CHARS * 0.55:
            break
    return True


def builtin_tools(cwd_fn):
    """Built-in tools as Tool objects (cwd_fn returns the current working directory)."""
    from .registry import Tool
    return [Tool(s["function"]["name"], s["function"]["description"], s["function"]["parameters"],
                 (lambda n: (lambda **kw: run(n, kw, cwd_fn())))(s["function"]["name"]), tags={"builtin"})
            for s in SCHEMAS]
