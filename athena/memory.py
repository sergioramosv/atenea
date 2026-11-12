"""Long-term memory: what Atenea knows about the user (shared by every conversation, web and Telegram),
plus per-project instructions read from ATENEA.md (like CLAUDE.md in Claude Code)."""
from __future__ import annotations

import os
import threading
import time

from . import config as C
from .registry import Tool

PATH = os.path.join(os.path.dirname(C.PATH), "memory.md")
PROJECT_FILES = ("ATENEA.md", "AGENTS.md", "CLAUDE.md")
_lock = threading.Lock()


def load() -> list[str]:
    try:
        lines = open(PATH, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return []
    return [ln[2:].strip() for ln in lines if ln.startswith("- ") and ln[2:].strip()]


def save(facts: list[str]):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    body = "# What Atenea remembers about Sergio\n\n" + "".join(f"- {f}\n" for f in facts)
    open(PATH, "w", encoding="utf-8").write(body)


def add(fact: str) -> str:
    fact = " ".join(fact.split())[:500]
    with _lock:
        facts = load()
        if any(fact.lower() == f.lower() for f in facts):
            return "Already remembered."
        facts.append(fact)
        save(facts)
    return f"Remembered ({len(facts)} facts in memory)."


def forget(text: str) -> str:
    t = text.lower().strip()
    with _lock:
        facts = load()
        keep = [f for f in facts if t not in f.lower()]
        save(keep)
    n = len(facts) - len(keep)
    return f"Forgot {n} fact(s)." if n else "Nothing in memory matched that."


def block(facts: list[str] | None = None) -> str:
    facts = load() if facts is None else facts
    if not facts:
        return ""
    return ("\n# Memory: what you know about the user (from earlier conversations; use it naturally, don't recite it)\n"
            + "".join(f"- {f}\n" for f in facts[-200:]))


def project_block(cwd: str) -> str:
    for name in PROJECT_FILES:
        p = os.path.join(cwd, name)
        if os.path.isfile(p):
            try:
                txt = open(p, encoding="utf-8").read()[:20000]
            except OSError:
                continue
            return f"\n# Project instructions ({name} in {cwd}) — follow them\n{txt}\n"
    return ""


INSTRUCTIONS = """- MEMORY: when the user asks you to remember something ("acuérdate de…", "remember…") or tells you a durable
  fact or preference about themselves, their work, people, accounts or projects, save it with `remember` (one short,
  self-contained sentence, written in third person, e.g. "Sergio's main client is Tribbu"). Use `forget` when asked to.
  Don't save secrets (passwords, tokens) or trivial one-off details.
- If the project has no ATENEA.md and the user asks you to "init" or document the project, create ATENEA.md with the
  build/test commands, structure and conventions (like CLAUDE.md)."""


def tools() -> list[Tool]:
    return [
        Tool("remember", "Save a durable fact about the user to long-term memory (shared by all conversations).",
             {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]},
             lambda fact: add(fact)),
        Tool("forget", "Remove facts from long-term memory that contain the given text.",
             {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
             lambda text: forget(text)),
    ]
