"""Copias de seguridad de Atenea: chats, memoria, configuración, artefactos e imágenes (~/.config/atenea) en un .tar.gz
diario en otro disco. Se conservan las últimas 30.  Uso: python -m atenea.backup  (o desde Ajustes → Copias)."""
from __future__ import annotations

import os
import sys
import tarfile
import time

from . import config as C

DEST = os.environ.get("ATENEA_BACKUP_DIR", "/mnt/models/backups/atenea")
KEEP = 30
SKIP = ("background", "vision.log", "cli_history")


def make() -> dict:
    src = os.path.dirname(C.PATH)
    os.makedirs(DEST, exist_ok=True)
    name = f"atenea-{time.strftime('%Y-%m-%d_%H%M')}.tar.gz"
    tmp = os.path.join(DEST, name + ".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(src, arcname="atenea",
                filter=lambda ti: None if any(f"/{s}" in ti.name or ti.name.endswith(s) for s in SKIP) else ti)
    os.replace(tmp, os.path.join(DEST, name))
    os.chmod(os.path.join(DEST, name), 0o600)
    files = sorted(f for f in os.listdir(DEST) if f.startswith("atenea-") and f.endswith(".tar.gz"))
    for old in files[:-KEEP]:
        os.remove(os.path.join(DEST, old))
    return {"file": os.path.join(DEST, name), "size": os.path.getsize(os.path.join(DEST, name)), "kept": min(len(files), KEEP)}


def listing() -> list[dict]:
    if not os.path.isdir(DEST):
        return []
    out = []
    for f in sorted(os.listdir(DEST), reverse=True):
        if f.startswith("atenea-") and f.endswith(".tar.gz"):
            p = os.path.join(DEST, f)
            out.append({"name": f, "size": os.path.getsize(p), "time": os.path.getmtime(p)})
    return out


if __name__ == "__main__":
    r = make()
    print(f"Copia creada: {r['file']} ({r['size'] / 1e6:.1f} MB) · se guardan las últimas {KEEP}")
    sys.exit(0)
