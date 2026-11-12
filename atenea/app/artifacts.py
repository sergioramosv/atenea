"""Artifacts: self-contained HTML pages Atenea creates (reports, dashboards, charts, mini apps, documents).

Stored in ~/.config/atenea/artifacts/<id>/: index.html (current), v<N>.html (previous versions) and meta.json.
The web panel shows them in a side panel inside a sandboxed iframe, and serves them at /a/<id>."""
from __future__ import annotations

import json
import os
import re
import time
import uuid

from .. import config as C
from ..registry import Tool

DIR = os.path.join(os.path.dirname(C.PATH), "artifacts")

INSTRUCTIONS = """- You can create ARTIFACTS with `create_artifact`: real web pages the user sees in a side panel of the panel
  (reports, dashboards, charts, tables, timelines, slides, mini apps, games, documents). Use one whenever a visual,
  interactive or long structured result is better than chat text, or when the user asks for a page/app/visualization.
  An artifact is ONE complete self-contained HTML file: inline <style> and <script>; libraries only from
  https://cdn.jsdelivr.net or https://cdnjs.cloudflare.com (e.g. Chart.js, D3, Mermaid, Leaflet, Three.js). Make it look
  professional: clean layout, good typography (system-ui or Geist from Google Fonts), works on phone and desktop, light and
  dark (prefers-color-scheme). Use real data you gathered, never placeholders. For big pages, first write the HTML with
  write_file and then call create_artifact with `path`. To change an existing artifact pass its `id`.
  After creating it, put [[artifact:ID]] on its own line in your reply where the card should appear.
- To SHOW an image or video that exists on this machine (screenshots, plots you generated, photos, recordings), write
  markdown with its absolute path: ![short description](/home/sergio/path/file.png) or ![video](/path/clip.mp4).
  Web images work too: ![description](https://...). You can create images with Python (matplotlib, Pillow) or ffmpeg."""


def _meta_path(aid: str) -> str:
    return os.path.join(DIR, aid, "meta.json")


def valid_id(aid: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]{6,16}", aid or ""))


def get(aid: str) -> dict | None:
    if not valid_id(aid):
        return None
    try:
        return json.load(open(_meta_path(aid), encoding="utf-8"))
    except FileNotFoundError:
        return None


def save(title: str, html: str, aid: str | None = None, chat: str | None = None) -> dict:
    meta = get(aid) if aid else None
    now = time.time()
    if meta is None:
        aid = uuid.uuid4().hex[:10]
        meta = {"id": aid, "title": title or "Untitled", "chat": chat, "created": now, "version": 0}
    d = os.path.join(DIR, aid)
    os.makedirs(d, exist_ok=True)
    cur = os.path.join(d, "index.html")
    if os.path.exists(cur):
        os.replace(cur, os.path.join(d, f"v{meta['version']}.html"))
    open(cur, "w", encoding="utf-8").write(html)
    meta.update(title=title or meta["title"], updated=now, version=meta["version"] + 1, size=len(html))
    json.dump(meta, open(_meta_path(aid), "w", encoding="utf-8"), ensure_ascii=False)
    return meta


def file_for(aid: str, version: int | None = None) -> str | None:
    meta = get(aid)
    if not meta:
        return None
    if version and version != meta["version"]:
        f = os.path.join(DIR, aid, f"v{version}.html")
    else:
        f = os.path.join(DIR, aid, "index.html")
    return f if os.path.isfile(f) else None


def make_tool(cwd_fn, chat: str | None = None, on_create=None) -> Tool:
    def create_artifact(title: str, html: str = "", path: str = "", id: str = "") -> str:
        if not html and path:
            p = path if os.path.isabs(path) else os.path.join(cwd_fn(), path)
            try:
                html = open(p, encoding="utf-8").read()
            except OSError as e:
                return f"Error: could not read {path}: {e}"
        if not html.strip():
            return "Error: pass the page as `html`, or write it to a file first and pass `path`."
        if "<html" not in html.lower() and "<body" not in html.lower():
            html = ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' "
                    "content='width=device-width,initial-scale=1'></head><body>" + html + "</body></html>")
        if id and not get(id):
            return f"Error: there is no artifact with id {id}. Omit `id` to create a new one."
        meta = save(title, html, id or None, chat)
        if on_create:
            on_create(meta)
        verb = "Updated" if meta["version"] > 1 else "Created"
        return (f"{verb} artifact {meta['id']} (version {meta['version']}, {len(html):,} bytes). "
                f"Put [[artifact:{meta['id']}]] on its own line in your reply so the user can open it.")

    return Tool("create_artifact",
                "Create or update an artifact: a complete, self-contained HTML page shown to the user in a side panel "
                "(reports, dashboards, charts, mini apps, documents). Give `html` directly, or `path` to an .html file "
                "you wrote. Pass `id` to update an existing artifact.",
                {"type": "object", "properties": {
                    "title": {"type": "string", "description": "Short title shown on the card"},
                    "html": {"type": "string", "description": "The full HTML document"},
                    "path": {"type": "string", "description": "Path of an .html file to publish instead of `html`"},
                    "id": {"type": "string", "description": "Id of an artifact to update (optional)"}},
                 "required": ["title"]},
                create_artifact)
