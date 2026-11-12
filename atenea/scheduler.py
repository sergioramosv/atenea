"""Scheduled tasks: "every weekday at 8:00 summarise my email". Stored in ~/.config/atenea/schedules.json.

`when` formats (local time):  "daily 08:00" · "weekdays 08:30" · "weekly mon 09:00" · "monthly 1 09:00" ·
"every 30m" · "every 2h" · "once 2026-11-20 18:00"."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import time
import uuid

from . import config as C
from .registry import Tool

PATH = os.path.join(os.path.dirname(C.PATH), "schedules.json")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ES = {"lun": "mon", "mar": "tue", "mie": "wed", "mié": "wed", "jue": "thu", "vie": "fri", "sab": "sat", "sáb": "sat", "dom": "sun"}
_lock = threading.Lock()


def load() -> list[dict]:
    try:
        return json.load(open(PATH, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save(items: list[dict]):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    json.dump(items, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def _hm(s: str) -> tuple[int, int]:
    h, m = (s.split(":") + ["0"])[:2]
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("bad time")
    return h, m


def next_run(when: str, after: float) -> float | None:
    """Next run time (epoch) strictly after `after`, or None when it will not run again."""
    w = when.lower().strip()
    now = dt.datetime.fromtimestamp(after)
    if m := re.fullmatch(r"every\s+(\d+)\s*(m|min|h|hour|hours|minutes)", w):
        n = int(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
        return after + max(5, n) * 60
    if m := re.fullmatch(r"once\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", w):
        t = dt.datetime.fromisoformat(f"{m.group(1)} {m.group(2)}").timestamp()
        return t if t > after else None
    parts = w.split()
    if not parts:
        raise ValueError("empty schedule")
    kind = parts[0]
    if kind in ("daily", "weekdays") and len(parts) == 2:
        h, mi = _hm(parts[1])
        for d in range(0, 8):
            c = (now + dt.timedelta(days=d)).replace(hour=h, minute=mi, second=0, microsecond=0)
            if c.timestamp() > after and (kind == "daily" or c.weekday() < 5):
                return c.timestamp()
    if kind == "weekly" and len(parts) == 3:
        day = ES.get(parts[1][:3], parts[1][:3])
        if day not in DAYS:
            raise ValueError("bad day")
        h, mi = _hm(parts[2])
        for d in range(0, 8):
            c = (now + dt.timedelta(days=d)).replace(hour=h, minute=mi, second=0, microsecond=0)
            if c.timestamp() > after and c.weekday() == DAYS.index(day):
                return c.timestamp()
    if kind == "monthly" and len(parts) == 3:
        day, (h, mi) = int(parts[1]), _hm(parts[2])
        for k in range(0, 14):
            y, mo = now.year + (now.month - 1 + k) // 12, (now.month - 1 + k) % 12 + 1
            try:
                c = dt.datetime(y, mo, day, h, mi)
            except ValueError:
                continue
            if c.timestamp() > after:
                return c.timestamp()
    raise ValueError(f"Unknown schedule '{when}'. Use: daily HH:MM · weekdays HH:MM · weekly mon HH:MM · "
                     "monthly 1 HH:MM · every 30m · every 2h · once YYYY-MM-DD HH:MM")


def add(name: str, prompt: str, when: str, mode: str = "auto", chat: str | None = None, kind: str = "task",
        flow: dict | None = None) -> dict:
    """kind: "task" (Atenea runs `prompt`), "reminder" (just a notification with `prompt` as text) or "flow"
    (a connector collects data first; Atenea only runs `prompt` on what is new; see atenea/app/flows.py)."""
    nr = next_run(when, time.time())
    item = {"id": uuid.uuid4().hex[:8], "name": name[:80], "prompt": prompt, "when": when, "mode": mode,
            "kind": kind if kind in ("reminder", "flow") else "task", "chat": chat, **({"flow": flow} if flow else {}), "enabled": True, "next": nr,
            "last": None, "last_chat": None, "history": [], "created": time.time()}
    with _lock:
        items = load()
        items.append(item)
        save(items)
    return item


def update(sid: str, **kw) -> dict | None:
    with _lock:
        items = load()
        for it in items:
            if it["id"] == sid:
                it.update({k: v for k, v in kw.items() if v is not None})
                if "when" in kw or kw.get("enabled"):
                    it["next"] = next_run(it["when"], time.time())
                save(items)
                return it
    return None


def remove(sid: str) -> bool:
    with _lock:
        items = load()
        keep = [i for i in items if i["id"] != sid]
        save(keep)
    return len(keep) != len(items)


def due(now: float) -> list[dict]:
    """Pop the tasks that are due, advancing their next run."""
    out = []
    with _lock:
        items = load()
        for it in items:
            if it.get("enabled") and it.get("next") and it["next"] <= now:
                out.append(dict(it))
                it["last"] = now
                try:
                    it["next"] = next_run(it["when"], now + 1)
                except ValueError:
                    it["next"] = None
                if it["next"] is None:
                    it["enabled"] = False
        if out:
            save(items)
    return out


def record(sid: str, entry: dict):
    """Append a run to the item's history (last 50)."""
    with _lock:
        items = load()
        for it in items:
            if it["id"] == sid:
                it["history"] = ([entry] + (it.get("history") or []))[:50]
                if entry.get("chat"):
                    it["last_chat"] = entry["chat"]
                save(items)
                return


def describe(it: dict) -> str:
    nxt = time.strftime("%a %d %b %H:%M", time.localtime(it["next"])) if it.get("next") else "—"
    return f"{it['id']}  {'on ' if it.get('enabled') else 'off'}  {it['when']:<18} next {nxt}  · {it['name']}"


def tools() -> list[Tool]:
    def schedule_task(name: str, prompt: str, when: str) -> str:
        try:
            it = add(name, prompt, when)
        except ValueError as e:
            return f"Error: {e}"
        return f"Scheduled: {describe(it)}\nIt appears in the Tasks space of the panel, where each run is kept (and on Telegram if set up)."

    def set_reminder(text: str, when: str) -> str:
        try:
            it = add(text[:60], text, when, kind="reminder")
        except ValueError as e:
            return f"Error: {e}"
        return f"Reminder set: {describe(it)}\nThe user will get a notification on their phone at that time."

    def list_scheduled() -> str:
        return "\n".join(describe(i) for i in load()) or "No scheduled tasks."

    def cancel_scheduled(id: str) -> str:
        return "Deleted." if remove(id) else f"No scheduled task {id}."

    return [
        Tool("schedule_task", "Schedule a recurring or one-off task that Atenea will run on her own (e.g. a morning "
             "email summary). `prompt` is the full instruction to run each time. `when`: 'daily 08:00', "
             "'weekdays 08:30', 'weekly mon 09:00', 'monthly 1 09:00', 'every 30m', 'every 2h', "
             "'once 2026-11-20 18:00' (local time).",
             {"type": "object", "properties": {"name": {"type": "string"}, "prompt": {"type": "string"},
                                               "when": {"type": "string"}}, "required": ["name", "prompt", "when"]},
             schedule_task),
        Tool("set_reminder", "Remind the user of something with a phone notification at a given time (no work for you "
             "then). `text`: what to remind, in the user's language. `when`: 'once 2026-11-20 09:00' for a single "
             "reminder (compute the date from today), or recurring like 'daily 08:00', 'weekdays 08:30', "
             "'weekly mon 09:00', 'monthly 1 09:00', 'every 2h'. Use it for 'recuérdame…'.",
             {"type": "object", "properties": {"text": {"type": "string"}, "when": {"type": "string"}},
              "required": ["text", "when"]}, set_reminder),
        Tool("list_scheduled", "List scheduled tasks and reminders.", {"type": "object", "properties": {}}, list_scheduled),
        Tool("cancel_scheduled", "Delete a scheduled task by id.",
             {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, cancel_scheduled),
    ]
