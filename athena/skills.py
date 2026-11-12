"""Skills: specialised instructions stored in files, loaded by the agent only when needed.

Layout (compatible with the Claude Code skill format):
    skills/<name>/SKILL.md
        ---
        name: review-pr
        description: How to review a pull request in this team
        ---
        (Markdown instructions)

Only the catalog (name + description) goes into the system prompt; the body is loaded with load_skill.
"""
from __future__ import annotations

import os
import re

from .registry import Tool


class SkillSet:
    def __init__(self, *dirs: str):
        self.skills: dict[str, dict] = {}
        for d in dirs:
            d = os.path.expanduser(d)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                f = os.path.join(d, name, "SKILL.md")
                if os.path.isfile(f):
                    self._add(f)

    def _add(self, path: str):
        text = open(path, encoding="utf-8").read()
        meta, body = {}, text
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
        if m:
            body = m.group(2)
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
        name = meta.get("name") or os.path.basename(os.path.dirname(path))
        self.skills[name] = {"description": meta.get("description", ""), "body": body.strip(), "path": path}

    def __len__(self):
        return len(self.skills)

    def catalog(self) -> str:
        lines = [f"  - {n}: {s['description']}" for n, s in self.skills.items()]
        return "- Available skills (load one with load_skill before a task of that kind):\n" + "\n".join(lines)

    def tool(self) -> Tool:
        def load_skill(name: str) -> str:
            s = self.skills.get(name)
            if not s:
                return f"Skill '{name}' does not exist. Available: {', '.join(self.skills)}"
            return f"# Skill: {name}\n{s['body']}"
        return Tool("load_skill", "Load the full instructions of a skill from the catalog.",
                    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, load_skill)
