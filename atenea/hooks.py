"""Hooks: shell commands that run on agent events, like Claude Code hooks. Configured in config.json:

  "hooks": {
    "PreToolUse":  [{"matcher": "bash|edit_file", "command": "my-check.sh"}],   # exit 2 = block (stderr = reason)
    "PostToolUse": [{"matcher": "edit_file|write_file", "command": "ruff format \"$ATENEA_FILE\""}],
    "UserPromptSubmit": [{"command": "date"}],          # stdout is added to the message as context; exit 2 = block
    "Stop": [{"command": "pytest -q"}]                  # exit 2 = not finished: stderr goes back to Atenea
  }

Each command gets the event as JSON on stdin and env vars ATENEA_EVENT, ATENEA_TOOL, ATENEA_FILE, ATENEA_CWD."""
from __future__ import annotations

import json
import os
import re
import subprocess

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")


def run(hooks: dict | None, event: str, cwd: str, tool: str = "", payload: dict | None = None) -> list[tuple[int, str, str, str]]:
    """Run the matching hooks. Returns [(exit_code, stdout, stderr, command)]."""
    out = []
    for h in (hooks or {}).get(event) or []:
        cmd, matcher = h.get("command"), h.get("matcher") or "*"
        if not cmd:
            continue
        if tool and matcher != "*" and not re.fullmatch(matcher, tool):
            continue
        p = payload or {}
        env = {**os.environ, "ATENEA_EVENT": event, "ATENEA_TOOL": tool, "ATENEA_CWD": cwd,
               "ATENEA_FILE": str((p.get("args") or {}).get("path", ""))}
        try:
            r = subprocess.run(cmd, shell=True, cwd=cwd, input=json.dumps({"event": event, "tool": tool, **p}, default=str),
                               capture_output=True, text=True, timeout=int(h.get("timeout", 120)), env=env,
                               executable="/bin/bash")
            out.append((r.returncode, r.stdout[-4000:], r.stderr[-4000:], cmd))
        except subprocess.TimeoutExpired:
            out.append((1, "", "hook timed out", cmd))
    return out
