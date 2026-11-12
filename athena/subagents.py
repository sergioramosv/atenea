"""Named subagents (like Claude Code's agents): Markdown files with a small header and a system prompt.

  ---
  name: reviewer
  description: Reviews code changes for bugs and risky patterns
  tools: read_file, list_files, grep, bash        (optional: limit the tools)
  ---
  You are a meticulous code reviewer...

Loaded from athena/agents (built in), ~/.config/athena/agents and <project>/.atenea/agents (later ones win)."""
from __future__ import annotations

import os

from . import config as C

BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "agents")


def _parse(path: str) -> dict | None:
    try:
        txt = open(path, encoding="utf-8").read()
    except OSError:
        return None
    meta, body = {}, txt
    if txt.startswith("---"):
        end = txt.find("\n---", 3)
        if end > 0:
            for line in txt[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            body = txt[end + 4:].strip()
    name = meta.get("name") or os.path.basename(path)[:-3]
    tools = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()] or None
    return {"name": name, "description": meta.get("description", ""), "tools": tools, "prompt": body, "path": path}


def load(cwd: str | None = None) -> dict[str, dict]:
    dirs = [BUILTIN_DIR, os.path.join(os.path.dirname(C.PATH), "agents")]
    if cwd:
        dirs.append(os.path.join(cwd, ".atenea", "agents"))
    out = {}
    for d in dirs:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".md"):
                    a = _parse(os.path.join(d, f))
                    if a:
                        out[a["name"]] = a
    return out


def catalog(agents: dict) -> str:
    if not agents:
        return ""
    return ("- Specialised subagents you can pass to `delegate` as `agent` (otherwise a general one is used):\n"
            + "".join(f"  - {a['name']}: {a['description']}\n" for a in agents.values()))
